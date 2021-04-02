FROM python:3-slim

RUN pip3 install prometheus_client pyserial

ADD exporter.py /usr/local/bin/heliotherm_exporter

EXPOSE 9997/tcp

ENTRYPOINT [ "/usr/local/bin/heliotherm_exporter" ]
