FROM python:3-slim

RUN pip3 install prometheus_client pyserial

LABEL source_repository="https://github.com/neredera/heliotherm_exporter"

ADD exporter.py /usr/local/bin/heliotherm_exporter

EXPOSE 9997/tcp

ENTRYPOINT [ "/usr/local/bin/heliotherm_exporter" ]
