#!/usr/bin/env python3

import argparse
import serial
import time
import os
import logging
import binascii
import re

import prometheus_client

from typing import Optional
from dataclasses import dataclass
from prometheus_client.core import (
    InfoMetricFamily, GaugeMetricFamily, CounterMetricFamily, StateSetMetricFamily, Counter)

# TODO: Errorhandling, Recovery after errors (e.g. sensor temporary unavailable), error counters
# TODO: Logging for SystemD. More Logging.
# TODO: Own User/Group for Service

PROMETHEUS_NAMESPACE = 'heliotherm'

@dataclass
class DataValue:
    """Class for data values from the heat pump"""
    key: str    # e.g. M123, S23
    name: str   # N
    promname: str # Name converted to prometheus convention
    value: Optional[float]

    def data_read_command(self) -> bytes:
        return bytes(f'{self.key[0]}P,NR={self.key[1:]};', 'utf-8')

class HeliothermCollector(object):
    """Collector for sensor data."""

    PREFIX = b"\x7e"

    RESPONSE_TIMEOUT_SEC = 1    # how long to wait for a response

    lan_gateway = None
    lan_gateway_port = None

    known_data_values = {}

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

    def receiveAndDecode(self, port: serial.Serial, previous_data: bytes = b'', timeout = None, accept_no_response = False, expect_one_packet = False):
        """
        Receives and decodes packets.
        Returns the received packet and remaining data for a possible next packet arriving.
        The timeout is the time to wait when no (more) data arrives for a full packet.
        expect_one_packet: if this is the last/only packet.
        """
        REPLY_START = b'\x02\xfd\xe0\xd0'
        REPLY_COM = b'\x02\xfd\xe0\xd0\x00\x00'    # Reply Commando
        REPLY_COM_2 = b'\x02\xfd\xe0\xd0\x04\x00'  # Reply Commando 2 (e.g. for MP,NR=16). With this reply the CRC is 00
        REPLY_COM_3 = b'\x02\xfd\xe0\xd0\x02\x00'  # Reply Commando 3 (for error messages?). With this reply the length is 00 (but there is data and a (invalid?) crc)
        REPLY_COM_4 = b'\x02\xfd\xe0\xd0\x01\x00'  # Reply Commando 4 (?) With this reply the length is 00.

        if timeout is None:
            timeout = self.RESPONSE_TIMEOUT_SEC

        undecoded = previous_data

        wait_time = time.time() + timeout
        # this loop ends with at least a full packet or no data arrived for at least timeout seconds
        while time.time() <= wait_time:
            if len(undecoded) >= 8:
                # preamble and size is here
                size = undecoded[6]
                if size == 0:
                    # there are some packets with size 0 (wrongly).
                    # do we heve the start of a second packet?
                    if undecoded.find(REPLY_START, 7) > 0:
                        break
                    elif expect_one_packet:
                        # we do not expect another packet. Avoid timeout (with the small risk that we do not have the full packet)
                        break
                    else:
                        # read more or go into timeout to avoid that we cannot correctly detect the end of the packet.
                        size = 255
                if len(undecoded)>= size + 6 + 1 + 1:
                    # we have at least one complete packet
                    break

            data = port.read(1024)
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
        if REPLY_COM != com and REPLY_COM_2 != com and REPLY_COM_3 != com and REPLY_COM_4 != com:
            self.communication_errors.inc()
            logging.info(f'Unexpected preamble in received packet. Received: {com} Expected: {REPLY_COM}  Packet:{undecoded}')
            return (None, b'')    # flush likely corrupted data

        sent_data_length = undecoded[6]
        if len(undecoded) < sent_data_length + 6 + 1 + 1: # preamble (6), size byte (1), CRC (1)
            self.communication_errors.inc()
            logging.info(f'Not enough data received for size stated in received packet. DevicesSays: {sent_data_length} DataReceived: {len(undecoded) - 6 - 1}  Packet:{undecoded}')
            return (None, b'')    # flush likely corrupted data

        if sent_data_length == 0:
            if REPLY_COM_3 == com or REPLY_COM_4 == com:
                logging.debug(f'Alternative reply commando (3 or 4) received with length 0.')
                next_reply_pos =  undecoded.find(REPLY_START, 7) # look for the start of the next packet
                if next_reply_pos == -1:
                    logging.debug(f'Calculated length by accepting the received data (packet received with length 0).')
                    sent_data_length = len(undecoded) - 6 - 1 - 1 #we calculate the lenght assuming we have only one packet
                else:
                    logging.debug(f'Calculated length by looking for next packet (packet received with length 0).')
                    sent_data_length = next_reply_pos - 6 - 1 - 1
            else:
                self.communication_errors.inc()
                logging.info(f'Packet with length=0 received.  Packet:{undecoded}')
                return (None, b'')    # flush likely corrupted data

        sent_crc = undecoded[6 + 1 + sent_data_length]
        calc_crc = self.makeCrc(undecoded[ : 6+1+ sent_data_length ])[0]

        if sent_crc != calc_crc:
            if REPLY_COM_2 == com and sent_crc == 0:
                logging.debug(f'Alternative reply commando (2) received with CRC 0.')
            elif REPLY_COM_3 == com or REPLY_COM_4 == com:
                logging.debug(f'Alternative reply commando (3 or 4) received with invalid CRC.')
            else:
                self.communication_errors.inc()
                logging.info(f'CRC error for received packet. Sent from Device: {sent_crc} Calculated: {calc_crc}  Packet:{undecoded}')
                return (None, b'')    # flush likely corrupted data

        packet_data = undecoded[ 6+1 : 6+1+sent_data_length ]
        undecoded = undecoded[ 6+1+sent_data_length+1 : ]    # remove packet

        if packet_data[0] != self.PREFIX[0]:
            self.communication_errors.inc()
            logging.info(f'Unexpected prefix. Received: {packet_data[0]} Expected: {self.PREFIX[0]}  Packet:{packet_data}')
            return (None, undecoded)    # framing was ok, only prefix unexpected.
        
        payload = packet_data[1:]
        if len(payload)>2 and payload[-2:]==b'\r\n':    #strip CRLF
            payload = payload[:-2]

        logging.debug(f'Received payload: {payload}')

        return (payload, undecoded)

    def sendQuery(self, query_command, port: serial.Serial, timeout = None, accept_no_response = False):
        """
        Send command und return received data (max 1 packet).
        Adds and removes preamble, length, prefix and CRC.
        """

        data_to_send = self.prepareQuery(query_command)
        logging.debug(f'Sending query: {query_command}')
        logging.debug(f'Sending packet: {binascii.b2a_hex(data_to_send)}   {data_to_send}')

        port.write(data_to_send)

        (payload, undecoded) = self.receiveAndDecode(port, timeout=timeout, accept_no_response=accept_no_response, expect_one_packet=True)

        if payload is None:
            return []

        return payload

    def sendQueryMultiResults(self, query_command, port: serial.Serial, timeout = None, accept_no_response = False, expected_result_count = None):
        """
        Send command und return received data (multiple packets possible).
        Adds and removes preamble, length, prefix and CRC.
        """

        data_to_send = self.prepareQuery(query_command)
        logging.debug(f'Sending query: {query_command}')
        logging.debug(f'Sending packet: {binascii.b2a_hex(data_to_send)}   {data_to_send}')

        port.write(data_to_send)

        previous_data = b''
        received_packets = []

        while True:
            if not expected_result_count is None and len(received_packets) == expected_result_count-1:
                last_packet = True
            else:
                last_packet = False

            (payload, previous_data) = self.receiveAndDecode(port, previous_data=previous_data, expect_one_packet=last_packet)
            if not payload is None:
                logging.debug(f'Received packet {len(received_packets)}')
                received_packets.append(payload)

            if last_packet or payload is None:
                if (not accept_no_response) and len(received_packets) == 0:
                    self.communication_errors.inc()
                    logging.info(f'No packets received. command={query_command}')

                logging.debug('No further response received')

                if not expected_result_count is None:
                    if len(received_packets) != expected_result_count:
                        self.communication_errors.inc()
                        logging.info(f'Unexpected packet count received when reading multiple values with one command: expected={expected_result_count}, received={len(received_packets)}, command={query_command}')

                return received_packets

    def CreatePromMetric(self, data: DataValue):
        logging.info(f'nr={data.key}, name={data.name}, promname={data.promname}, value={data.value}')

        metric = GaugeMetricFamily(
            PROMETHEUS_NAMESPACE + '_' + data.promname,
            data.name)
        metric.add_metric(
            labels=[],
            value=data.value)
        return metric

    def collectHeliothermData(self):
        metrics = []

        # Interesting Values
        VALUES_TO_READ = [
            'M0', 'M1', 'M2', 'M3', 'M4', 'M5', 'M6', 'M8', 'M9', 'M12', 'M13', 'M14', 'M15', 'M18', 'M19',
            'M20', 'M21', 'M22', 'M23', 'M24', 'M25', 'M29', 'M30', 'M31', 'M32', 'M33', 'M34', 'M36', 'M37',
            'M38', 'M41', 'M47', 'M48',
            'M51', 'M52', 'M54', 'M56', 'M57', 'M58', 'M59', 'M61', 'M62', 
            'M63', 'M65', 'M66', 'M67', 'M68', 'M69', 'M71', 'M72', 'M73', 'M74',
            'S3', 'S9', 'S10', 'S11', 'S13', 'S14', 'S69', 'S76', 'S83', 'S85', 
            'S153', 'S155', 'S156', 'S158', 'S159', 'S161', 'S162', 'S164', 'S165', 'S167', 'S171', 'S172', 'S173', 'S200', 'S223']

        # Test Values (with first invalid values)
        #VALUES_TO_READ = ['M0', 'M16', 'M101', 'M102', 'M103', 'S0', 'S1', 'S223', 'S350', 'S401', 'S413', 'S414', 'S415', 'S416']

        # Read all Values
        #VALUES_TO_READ = [*map(lambda i : 'M' + str(i) , range(0, 104)), *map(lambda i : 'S' + str(i) , range(0, 417))]

        #port = serial.Serial(self.device, baudrate=38400, timeout=0.0,dsrdtr=True)
        with serial.serial_for_url(f"socket://{self.lan_gateway}:{self.lan_gateway_port}", timeout=0.01) as port:
            # Protocol description based on:
            # https://knx-user-forum.de/forum/%C3%B6ffentlicher-bereich/knx-eib-forum/code-schnipsel/40472-kommunikation-mit-heliotherm-w%C3%A4rmepumpe-n
            # and
            # https://github.com/dstrigl/htheatpump

            response_success = b'OK;'
            command_login = b'LIN;'
            command_logout = b'LOUT;'
            command_rm = b'RM;' # lists all MR values
            command_rs = b'RS;' # lists all SR values
            command_mr = b'MR,0,2,3,4,5,12,13,14,15,18,19,22,23,69,71,72,73,74,63,65,66,67,68,37,6,8,9,24,31,20,21,25,29,30,38,47,48,51,52,54,32,33;' # batch read M-Values
            command_set_sr = b'SP,NR=223,VAL=22;' # set SP Nr. 225 to 22°C

            connect_string = b'\r\nCONNECT 19200\r\n'  # Looks like we are faking a modem.

            result_login = self.sendQuery(command_login, port, accept_no_response=True, timeout=self.RESPONSE_TIMEOUT_SEC/2)
            if result_login != response_success:
                # if we get no response, send the connect string and try again.
                logging.info(f'Sent connect string (second login attempt)')
                port.write(connect_string)

                result_login = self.sendQuery(command_login, port)
                if result_login != response_success:
                    self.gathering_errors.inc()
                    logging.info(f'Login unsucessful')
                    return metrics
            logging.info(f'Login sucessful')

            #received_packets = self.sendQueryMultiResults(command_mr, port)

            # self.sendQuery(b'SP,NR=10;', port)
            # received_packets = self.sendQueryMultiResults(b'MR,0,1,2,3,4,5,6,8,9,12,13,14,15,18,19,20,21,22,23,24,25,29,30,31,32,33,37,38,47,48,51,52,54,56,63,65,66,67,68,69,71,72,73,74;', port)
            
            # if not received_packets is None:
            #     logging.info(f'Received {len(received_packets)} packets.')
            #     for packet in received_packets:
            #         logging.info(f'Packet: {packet}')

            multi_command = b'MR'
            multi_command_count = 0

            total_values_received = 0

            for value_key in VALUES_TO_READ:
                value_kind = value_key[0]
                value_nr = value_key[1:]

                data = self.known_data_values.get(value_key)
                if value_kind == 'M':
                    #we can read these with a multi_command if we have the names
                    if not data is None:
                        multi_command += b',' + bytes(value_nr, 'utf-8')
                        multi_command_count += 1
                        continue

                command = bytes(f'{value_kind}P,NR={value_nr};', 'utf-8')
                result = self.sendQuery(command, port)
                if len(result)<1:
                    continue
                result = result.decode()
                logging.debug(f'result={result}')

                if result.startswith('ERR,'):
                    self.communication_errors.inc()
                    logging.info(f'Error result received: {result}')
                    continue

                # example: MP,NR=0,ID=0,NAME=Temp. Aussen,LEN=4,TP=1,BIT=1,VAL=4.8,MAX=40.0,MIN=-20.0,ERF=0,ORV=0.0,ORF=0,TRF=1,TRT=0,TRHV=1.0,TRI=900,TE=31.12.99-11:59:00,OFFV=0.0,RT1=0,RTL=0,WR=1,US=1;
                try:
                    nr = result[0] + str(int(re.search(',NR=([0-9.-]*),', result).groups()[0]))
                    name = re.search(",NAME=([a-zA-Z0-9._():% -/]*),", result).groups()[0]
                    value = float(re.search(',VAL=([0-9.-]*),', result).groups()[0])
                except AttributeError:
                    self.communication_errors.inc()
                    logging.exception(f"Failed to parse result '{result}'.")
                    continue

                if nr != value_key:
                    self.communication_errors.inc()
                    logging.info(f'Different value then expected received: received={nr} expected={value_key}')
                    continue

                if data is None:
                    promname = name.lower().replace(' ', '_').replace('.', '_')
                    promname = promname.replace('(', '_').replace(')', '_')
                    promname = promname.replace(':', '_').replace('-', '_')
                    promname = promname.replace('%', '_prozent_').replace('/', '_pro_')
                    promname = promname.replace('__', '_').replace('__', '_').replace('__', '_').strip('_')

                    data = DataValue(value_key, name, promname, value)
                    self.known_data_values[value_key] = data
                else:
                    data.value = value

                metrics.append(self.CreatePromMetric(data))
                total_values_received += 1

            multi_command += b';'

            if multi_command_count>0:
                logging.debug(f'Reading multiple values with one command: {multi_command}')
                received_packets = self.sendQueryMultiResults(multi_command, port, expected_result_count = multi_command_count)

                if received_packets is None or len(received_packets)==0:
                    self.communication_errors.inc()
                    self.gathering_errors.inc()     # this concerns many values, we consider this a major error
                    logging.info(f'No packets received when reading multiple values with one command: {multi_command}')
                    return metrics

                for packet in received_packets:
                    # example: MA,3,24.1,37;
                    #          MA,NR,VAL,unknown
                    packet = packet.decode()
                    groups = re.search('MA,([0-9]*),([0-9.-]*),', packet).groups()
                    nr = int(groups[0])
                    val = float(groups[1])
                    value_key = "M" + str(nr)
                    data = self.known_data_values.get(value_key)
                    if data is None:
                        self.communication_errors.inc()
                        logging.info(f'Unexpected received value with no existing data. value_key={value_key}')
                        continue
                    data.value = val
                    metrics.append(self.CreatePromMetric(data))
                    total_values_received += 1

            result_logout = self.sendQuery(command_logout, port)
            if result_logout != response_success:
                logging.info(f'Logout unsucessful')

        if len(VALUES_TO_READ) != total_values_received:
            logging.info(f'Could not read all data values. expected={len(VALUES_TO_READ)}, received={total_values_received}')

        metric_total_values_expected = GaugeMetricFamily(
            PROMETHEUS_NAMESPACE + '_total_values_expected',
            "Total values expected in this gathering run.")
        metric_total_values_expected.add_metric(
            labels=[],
            value=len(VALUES_TO_READ))
        metrics.append(metric_total_values_expected)            

        metric_total_values_received = GaugeMetricFamily(
            PROMETHEUS_NAMESPACE + '_total_values_received',
            "Total values received in this gathering run.")
        metric_total_values_received.add_metric(
            labels=[],
            value=total_values_received)
        metrics.append(metric_total_values_received)            

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
