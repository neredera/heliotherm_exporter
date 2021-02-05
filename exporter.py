#!/usr/bin/env python3

import argparse
import serial
import time
import os
import logging
import binascii
import re

import prometheus_client

from prometheus_client.core import (
    InfoMetricFamily, GaugeMetricFamily, CounterMetricFamily, StateSetMetricFamily, Counter)

# TODO: Errorhandling, Recovery after errors (e.g. sensor temporary unavailable), error counters
# TODO: Logging for SystemD. More Logging.
# TODO: Own User/Group for Service

PROMETHEUS_NAMESPACE = 'heliotherm'

class HeliothermCollector(object):
    """Collector for sensor data."""

    PREFIX = b"\x7e"

    RESPONSE_TIMEOUT_SEC = 1    # how long to wait for a response

    lan_gateway = None
    lan_gateway_port = None

    gathering_errors = Counter('gathering_errors', 'Amount of gathering runs with failures', labelnames=[], namespace=PROMETHEUS_NAMESPACE)
    communication_errors = Counter('communication_errors', 'Amount of communication errors with the heliotherm heat pump', labelnames=[], namespace=PROMETHEUS_NAMESPACE)

    def __init__(self, lan_gateway, lan_gateway_port, registry=prometheus_client.REGISTRY):
        self.lan_gateway = lan_gateway
        self.lan_gateway_port = lan_gateway_port

        registry.register(self)

    def makeCrc(self, data: bytes):
        crc = 0x00
        for byte in data:
            crc = crc ^ byte
            byte = (byte << 1) & 0xff
            crc = crc ^ byte
        crc = crc & 0xff
        return bytes([crc])

    def prepareQuery(self, query_command):
        """
        Send command und return received data.
        Adds preamble, length, prefix and CRC.
        """
        CTRL_COM  = b'\x02\xfd\xd0\xe0\x00\x00'  # Control Commando
        data = CTRL_COM + bytes([len(query_command)+1]) + self.PREFIX + query_command
        data_to_send = data + self.makeCrc(data)
        return data_to_send

    def receiveAndDecode(self, port: serial.Serial, previous_data = b'', timeout = None, accept_no_response = False):
        """
        Receives and decodes packets.
        Returns the received packet and remaining data for a possible next packet arriving.
        The timeout is the time to wait when no (more) data arrives for a full packet.
        """

        REPLY_COM = b'\x02\xfd\xe0\xd0\x00\x00'  # Reply Commando
        REPLY_COM_2 = b'\x02\xfd\xe0\xd0\x04\x00'  # Reply Commando 2 (e.g. for MP,NR=16). With this reply the CRC is 00
        REPLY_COM_3 = b'\x02\xfd\xe0\xd0\x02\x00'  # Reply Commando 3 (for error messages?). With this reply the length is 00 (but there is data and a (invalid?) crc)

        if timeout is None:
            timeout = self.RESPONSE_TIMEOUT_SEC

        undecoded = previous_data

        wait_time = time.time() + timeout
        # this loop ends with at least a full packet or no data arrived for at least timeout seconds
        while time.time() <= wait_time:
            if len(undecoded) >= 8:
                # preamble and size is here
                size = undecoded[6]
                if len(undecoded)>= size + 6 + 1 + 1:
                    # we have at least one complete packet
                    break

            data = port.read(1014)
            if len(data)>0:
                wait_time = time.time() + timeout # reset timeout
            undecoded += data

        if len(undecoded) > 0:
            logging.debug(f'Received data: {binascii.b2a_hex(undecoded)}   {undecoded}')
        else:
            if not accept_no_response:
                self.communication_errors.inc()
                logging.info(f'Received no data')
            return (None, b'')    # nothing received

        if len(undecoded) < 8:
            self.communication_errors.inc()
            logging.info(f'Not enough data received for a full packet. Packet: {undecoded}')
            return (None, b'')    # flush likely corrupted data

        com = undecoded[:6]
        if REPLY_COM != com and REPLY_COM_2 != com and REPLY_COM_3 != com:
            self.communication_errors.inc()
            logging.info(f'Unexpected preamble in received packet. Received: {com} Expected: {REPLY_COM}  Packet:{undecoded}')
            return (None, b'')    # flush likely corrupted data

        sent_data_length = undecoded[6]
        if len(undecoded) < sent_data_length + 6 + 1 + 1: # preamble (6), size byte (1), CRC (1)
            self.communication_errors.inc()
            logging.info(f'Not enough data received for size stated in received packet. DevicesSays: {sent_data_length} DataReceived: {len(undecoded) - 6 - 1}  Packet:{undecoded}')
            return (None, b'')    # flush likely corrupted data

        if sent_data_length == 0:
            if REPLY_COM_3 == com:
                logging.debug(f'Alternative reply commando (3) received with length 0.')
                sent_data_length = len(undecoded) - 6 - 1 - 1 #we calculate the lenght assuming we have only one packet
            else:
                self.communication_errors.inc()
                logging.info(f'Packet with length=0 received.  Packet:{undecoded}')
                return (None, b'')    # flush likely corrupted data

        sent_crc = undecoded[6 + 1 + sent_data_length]
        packet_data = undecoded[6 + 1:sent_data_length]
        calc_crc = self.makeCrc(packet_data)[0]

        if sent_crc != calc_crc:
            if REPLY_COM_2 == com and sent_crc == 0:
                logging.debug(f'Alternative reply commando (2) received with CRC 0.')
            elif REPLY_COM_3 == com and sent_crc == 117:
                logging.debug(f'Alternative reply commando (3) received with CRC 117.')
            else:
                self.communication_errors.inc()
                logging.info(f'CRC error for received packet. Sent from Device: {sent_crc} Calculated: {calc_crc}  Packet:{undecoded}')
                return (None, b'')    # flush likely corrupted data

        undecoded = undecoded[6 + 1 + sent_data_length + 1 :]    # remove packet

        if packet_data[0] != self.PREFIX[0]:
            self.communication_errors.inc()
            logging.info(f'Unexpected prefix. Received: {packet_data[0]} Expected: {self.PREFIX[0]}  Packet:{packet_data}')
            return (None, undecoded)    # framing was ok, only prefix unexpected.
        
        payload = packet_data[1:]
        if len(payload)>2 and payload[-2:]==b'\r\n':    #strip CRLF
            payload = payload[:-2]

        logging.debug(f'Received payload: {payload}')

        return (payload, undecoded)

    def sendBatchQuery(self, query_commands, port: serial.Serial):
        """
        Send up to two command next to each other.
        You can send only commands that return exactly one message.
        """

        data_to_send = []
        for command in query_commands:
            data_to_send.append(self.prepareQuery(command))

        port.write(data_to_send[0])
        send_index = 1

        while send_index < len(data_to_send):
            port.write(data_to_send[send_index])
            send_index += 1


        port.write(data_to_send)

        timeout = time.time() + self.RESPONSE_TIMEOUT_SEC
        read = b''
        while time.time() <= timeout:
            read += port.read(1000)
        
        read_in_hex = binascii.b2a_hex(read)
        logging.info(f'Received batch data: {read_in_hex}   {read}')

    def sendQuery(self, query_command, port: serial.Serial, timeout = None, accept_no_response = False):
        """
        Send command und return received data.
        Adds and removes preamble, length, prefix and CRC.
        """

        REPLY_COM = b'\x02\xfd\xe0\xd0\x00\x00'  # Reply Commando
        REPLY_COM_2 = b'\x02\xfd\xe0\xd0\x04\x00'  # Reply Commando 2 (e.g. for MP,NR=16). With this reply the CRC is 00
        REPLY_COM_3 = b'\x02\xfd\xe0\xd0\x02\x00'  # Reply Commando 3 (for error messages?). With this reply the length is 00 (but there is data and a (invalid?) crc)

        if timeout is None:
            timeout = self.RESPONSE_TIMEOUT_SEC

        data_to_send = self.prepareQuery(query_command)

        logging.debug(f'Sending query: {query_command}')
        logging.debug(f'Sending packet: {binascii.b2a_hex(data_to_send)}   {data_to_send}')

        port.write(data_to_send)

        #(payload, undecoded) = self.receiveAndDecode(port, timeout=timeout, accept_no_response=accept_no_response)

        wait_time = time.time() + timeout
        read = b''
        while time.time() <= wait_time:
            read += port.read(1000)
            if len(read) >= 7:
                # preamble and size is here
                size = read[6]
                if len(read)>= size + 6 + 1 + 1:
                    break

        if len(read) > 0:
            read_in_hex = binascii.b2a_hex(read)
            logging.debug(f'Received packet: {read_in_hex}   {read}')

            if len(read) < 8:
                self.communication_errors.inc()
                logging.info(f'Received packet too short. Packet:{read_in_hex}')
                return []

            com = read[:6]
            if REPLY_COM != com and REPLY_COM_2 != com and REPLY_COM_3 != com:
                self.communication_errors.inc()
                logging.info(f'Unexpected preamble in received packet. Received: {com} Expected: {REPLY_COM}  Packet:{read_in_hex}')
                return []

            if read[6] != len(read) - 6 - 1 - 1:
                if REPLY_COM_3 == com and read[6] == 0:
                    logging.debug(f'Alternative reply commando (3) received with length 0.')
                else:
                    self.communication_errors.inc()
                    logging.info(f'Unexpected length in received packet. DevicesSays: {read[6]} DataReceived: {len(read) - 6 - 1}  Packet:{read_in_hex}     {read}')
                    return []

            if read[-1] != self.makeCrc(read[:-1])[0]:
                if REPLY_COM_2 == com and read[-1] == 0:
                    logging.debug(f'Alternative reply commando (2) received with CRC 0.')
                elif REPLY_COM_3 == com and read[-1] == 117:
                    logging.debug(f'Alternative reply commando (3) received with CRC 117.')
                else:
                    self.communication_errors.inc()
                    logging.info(f'CRC error for received packet. Received: {read[-1]} Expected: {self.makeCrc(read[:-1])[0]}  Packet:{read_in_hex}    {read}')
                    return []

            if read[7] != self.PREFIX[0]:
                self.communication_errors.inc()
                logging.info(f'Unexpected prefix. Received: {read[7]} Expected: {self.PREFIX[0]}  Packet:{read_in_hex}')
                return []
            
            payload = read[8:-1]

            if len(payload)>2 and payload[-2:]==b'\r\n':    #strip CRLF
                payload = payload[:-2]

            logging.debug(f'Received payload: {binascii.b2a_hex(payload)}   {payload}')

            return payload
        else:
            if not accept_no_response:
                self.communication_errors.inc()
                logging.info(f'Received no response')
            return []

    def collectHeliothermData(self):
        metrics = []

        label_keys = [
            ]
        label_values = [
            ]

        # Interresting Values
        VALUES_TO_READ = [
            'M0', 'M1', 'M2', 'M3', 'M4', 'M5', 'M6', 'M18', 'M19', 'M22', 'M31', 'M47', 'M48', 'M56', 'M63', 'M67', 'M69', 'M71', 'M72', 'M73', 'M74',
            'S10', 'S11', 'S13', 'S14', 'S69', 'S76', 'S153', 'S155', 'S171', 'S172', 'S173', 'S200', 'S223']

        # Test Values (with first invalid values)
        #VALUES_TO_READ = ['M0', 'M16', 'M101', 'M102', 'M103', 'S0', 'S1', 'S223', 'S350', 'S401', 'S413', 'S414', 'S415', 'S416']

        # Read all Values
        #VALUES_TO_READ = [*map(lambda i : 'M' + str(i) , range(0, 104)), *map(lambda i : 'S' + str(i) , range(0, 417))]

        #port = serial.Serial(self.device, baudrate=38400, timeout=0.0,dsrdtr=True)
        with serial.serial_for_url(f"socket://{self.lan_gateway}:{self.lan_gateway_port}", timeout=0.01) as port:
            # Protocol description:
            # https://knx-user-forum.de/forum/%C3%B6ffentlicher-bereich/knx-eib-forum/code-schnipsel/40472-kommunikation-mit-heliotherm-w%C3%A4rmepumpe-n

            response_success = b'OK;'
            command_login = b'LIN;'
            command_logout = b'LOUT;'
            command_rm = b'RM;' # lists all MR values
            command_rs = b'RS;' # lists all SR values
            command_mr = b'MR,0,2,3,4,5,12,13,14,15,18,19,22,23,69,71,72,73,74,63,65,66,67,68,37,6,8,9,24,31,20,21,25,29,30,38,47,48,51,52,54,32,33;' # batch read M-Values
            command_set_sr = b'SP,NR=223,VAL=22;' # set SP Nr. 225 to 22°C

            connect_string = b'\r\nCONNECT 19200\r\n'  # Looks like we are faking a modem.

            result_login = self.sendQuery(command_login, port, accept_no_response=True)
            if result_login != response_success:
                # if we get no response, send the connect string and try again.
                logging.info(f'Sent connect string (second login attempt)')
                port.write(connect_string)

                result_login = self.sendQuery(command_login, port)
                if result_login != response_success:
                    self.gathering_errors.inc()
                    logging.info(f'Login unsucessful')
                    return metrics

            #self.sendBatchQuery([b'MP,NR=0;', b'MP,NR=1;', b'SP,NR=11;'], port)
            #self.sendBatchQuery([b'RS;'], port)

            for value in VALUES_TO_READ:
                command = bytes(f'{value[0]}P,NR={value[1:]};', 'utf-8')
                result = self.sendQuery(command, port)
                if len(result)<1:
                    continue
                result = result.decode()
                logging.debug(f'result={result}')

                if result.startswith('ERR,'):
                    logging.info(f'Error result received: {result}')
                    continue

                nr = float(re.search(',NR=([0-9.-]*),', result).groups()[0])
                name = re.search(",NAME=([a-zA-Z0-9._():% -/]*),", result).groups()[0]
                value = float(re.search(',VAL=([0-9.-]*),', result).groups()[0])

                promname = name.lower().replace(' ', '_').replace('.', '_')
                promname = promname.replace('(', '_').replace(')', '_')
                promname = promname.replace(':', '_').replace('-', '_')
                promname = promname.replace('%', '_prozent_').replace('/', '_pro_')
                promname = promname.replace('__', '_').replace('__', '_').replace('__', '_').strip('_')
                logging.info(f'nr={nr}, name={name}, promname={promname}, value={value}')

                heliotherm_value = GaugeMetricFamily(
                    PROMETHEUS_NAMESPACE + '_' + promname,
                    name,
                    labels=label_keys)
                heliotherm_value.add_metric(
                    labels=label_values, 
                    value=value)
                metrics.append(heliotherm_value)

            result_logout = self.sendQuery(command_logout, port)
            if result_logout != response_success:
                logging.info(f'Logout unsucessful')
                return metrics

        return metrics

    def collect(self):
          try:
            return self.collectHeliothermData()
          except:
            self.gathering_errors.inc()
            logging.exception("Failed to collect Heliotherm data.")
            return []

"""
# Adding known MP Numbers
$Command{Temp_Aussen}='MP,NR=0;';
$Command{Temp_Brauchwasser}='MP,NR=2;';
$Command{Temp_Vorlauf}='MP,NR=3;';
$Command{Temp_Ruecklauf}='MP,NR=4;';
$Command{Temp_EQ_Eintritt}='MP,NR=6;';
$Command{Temp_Frischwasser}='MP,NR=11;';
$Command{Temp_Verdampfung}='MP,NR=12;';
$Command{Temp_Kondensation}='MP,NR=13;';
$Command{Niederdruck}='MP,NR=20;';
$Command{Hochdruck}='MP,NR=21;';
$Command{Status_HKRPumpe}='MP,NR=22;';
$Command{Status_EQPumpe}='MP,NR=24;';
$Command{Status_Warmwasser}='MP,NR=25;';
$Command{Status_Verdichter}='MP,NR=30;';
$Command{Stoerung}='MP,NR=31;';
$Command{Vierwegeventil_Luft}='MP,NR=32;';
$Command{Status_Frischwasserpumpe}='MP,NR=50;';
$Command{Verdichteranforderung}='MP,NR=56;';
$Command{HKR_Soll}='MP,NR=57;';
# Adding known SP Numbers
$Command{ID}='SP,NR=9;';
$Command{Verdichter_Status}='SP,NR=10;';
$Command{Verdichter_Zeit}='SP,NR=11;';
$Command{Betriebsart}='SP,Nr=13;';
$Command{Entstoerung}='SP,Nr=14;';
$Command{HKR_Soll_Raum}='SP,NR=69;';
$Command{HKR_Aufheiztemp}='SP,NR=71;';
$Command{HKR_Absenktemp}='SP,NR=72;';
$Command{HKR_Heizgrenze}='SP,NR=76;';
$Command{Kurve_oHG}='SP,Nr=80;';
$Command{Kurve_0}='SP,Nr=81;';
$Command{Kurve_uHG}='SP,Nr=82;';
$Command{WW_Normaltemp}='SP,NR=83;';
$Command{WW_Minimaltemp}='SP,NR=85;';
$Command{Betriebsst_WW}='SP,Nr=171;';
$Command{Betriebsst_HKR}='SP,Nr=172;';
$Command{Betriebsst_ges}='SP,Nr=173;';
$Command{MKR2_aktiviert}='SP,Nr=222;';
$Command{Energiezaehler}='SP,Nr=263;';
"""


"""
        teslafi_info = InfoMetricFamily(
            PROMETHEUS_NAMESPACE,
            'TeslaFi car info (almost never changing)',
            value={
                'vin': self.getSetData(teslafi_data, teslafi_data_old, "vin"),
                'display_name': self.getSetData(teslafi_data, teslafi_data_old, "display_name"),
                'vehicle_id': self.getSetData(teslafi_data, teslafi_data_old, "vehicle_id"),
                'option_codes': self.getSetData(teslafi_data, teslafi_data_old, "option_codes"),
                'exterior_color': self.getSetData(teslafi_data, teslafi_data_old, "exterior_color"),
                'roof_color': self.getSetData(teslafi_data, teslafi_data_old, "roof_color"),
                'measure': self.getSetData(teslafi_data, teslafi_data_old, "measure"),
                'eu_vehicle': self.getSetData(teslafi_data, teslafi_data_old, "eu_vehicle"),
                'rhd': self.getSetData(teslafi_data, teslafi_data_old, "rhd"),
                'motorized_charge_port': self.getSetData(teslafi_data, teslafi_data_old, "motorized_charge_port"),
                'spoiler_type': self.getSetData(teslafi_data, teslafi_data_old, "spoiler_type"),
                'third_row_seats': self.getSetData(teslafi_data, teslafi_data_old, "third_row_seats"),
                'car_type': self.getSetData(teslafi_data, teslafi_data_old, "car_type"),
                'rear_seat_heaters': self.getSetData(teslafi_data, teslafi_data_old, "rear_seat_heaters"),
                })
        metrics.append(teslafi_info)
"""

if __name__ == '__main__':
    logging.basicConfig(level=os.environ.get("LOGLEVEL", "INFO"))
#    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

    PARSER = argparse.ArgumentParser()
    PARSER.add_argument("--port", help="The port where to expose the exporter (default:9997)", default=9997)
    PARSER.add_argument("--lan_gateway", help="Hostname or IP of the LAN to serial gateway")
    PARSER.add_argument("--lan_gateway_port", help="TCP port for the LAN gateway (default:4001)", default=4001)
    ARGS = PARSER.parse_args()

    port = int(ARGS.port)
    lan_gateway = str(ARGS.lan_gateway)
    lan_gateway_port = int(ARGS.lan_gateway_port)

    logging.info(f'Looking for Heliotherm heat pump on: {lan_gateway}:{lan_gateway_port}')

    HELIOTHERM_COLLECTOR = HeliothermCollector(lan_gateway, lan_gateway_port)

    logging.info("Starting exporter on port {}".format(port))
    prometheus_client.start_http_server(port)

    # sleep indefinitely
    while True:
        time.sleep(60)
