#!/usr/bin/python3
import base64
import json
import logging
import os
import ssl
import struct
from sys import stdout
from time import time

import paho.mqtt.client as mqtt
import redis
from opentelemetry import metrics
from opentelemetry.exporter.prometheus import PrometheusMetricReader
from opentelemetry.exporter.prometheus_remote_write import (
    PrometheusRemoteWriteMetricsExporter,
)
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation, View
from opentelemetry.sdk.resources import Resource
from prometheus_client import start_http_server

config = {
    "mqtt_address": os.environ.get("MQTT_ADDRESS", "mqtt.meshtastic.org"),
    "mqtt_use_tls": os.environ.get("MQTT_USE_TLS", False),
    "mqtt_port": os.environ.get("MQTT_PORT", 1883),
    "mqtt_keepalive": os.environ.get("MQTT_KEEPALIVE", 15),
    "mqtt_username": os.environ.get("MQTT_USERNAME"),
    "mqtt_password": os.environ.get("MQTT_PASSWORD"),
    "mqtt_topic": os.environ.get("MQTT_TOPIC", "helium/#"),
    "prometheus_endpoint": os.environ.get("PROMETHEUS_ENDPOINT"),
    "prometheus_token": os.environ.get("PROMETHEUS_TOKEN"),
    "prometheus_server_addr": os.environ.get("PROMETHEUS_SERVER_ADDR", "0.0.0.0"),
    "prometheus_server_port": os.environ.get("PROMETHEUS_SERVER_PORT", 9464),
    "redis_host": os.environ.get("REDIS_HOST", "localhost"),
    "redis_port": os.environ.get("REDIS_PORT", 6379),
    "log_level": os.environ.get("LOG_LEVEL", "INFO"),
    "flood_expire_time": os.environ.get("FLOOD_EXPIRE_TIME", 10 * 60),
}

logger = logging.getLogger(__name__)

logger.setLevel(getattr(logging, config["log_level"].upper()))

handler = logging.StreamHandler(stdout)
handler.setFormatter(
    logging.Formatter("%(asctime)s - lora_exporter - %(levelname)s - %(message)s")
)

logger.addHandler(handler)

headers = {}
if config["prometheus_token"]:
    headers = {
        "Authorization": f'Bearer {config["prometheus_token"]}',
    }

if config["prometheus_endpoint"] is None:
    reader = PrometheusMetricReader()
    start_http_server(
        port=config["prometheus_server_port"], addr=config["prometheus_server_addr"]
    )
else:
    exporter = PrometheusRemoteWriteMetricsExporter(
        endpoint=config["prometheus_endpoint"],
        headers=headers,
    )
    reader = PeriodicExportingMetricReader(exporter, 15000)

provider = MeterProvider(
    resource=Resource.create(attributes={"service.name": "lora"}),
    metric_readers=[reader],
    # views=[],
)
metrics.set_meter_provider(provider)
meter = metrics.get_meter(__name__)

lorawan_events_total = meter.create_counter(
    name="lorawan_events_total",
)

lorawan_rssi_decibel_milliwatts = meter.create_gauge(
    name="lorawan_rssi_decibel_milliwatts",
)

lorawan_snr_decibels = meter.create_gauge(
    name="lorawan_snr_decibels",
)

lorawan_temperature_celsius = meter.create_gauge(
    name="lorawan_temperature_celsius",
)

lorawan_humidity_percentage = meter.create_gauge(
    name="lorawan_humidity_percentage",
)

lorawan_battery_voltage_volts = meter.create_gauge(
    name="lorawan_battery_voltage_volts",
)


def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code.is_failure:
        logger.warning(
            f"Failed to connect to MQTT server with result code {reason_code}. loop_forever() will retry connection"
        )
    else:
        logger.info(f"Connected to MQTT server with result code {reason_code}")
        client.subscribe(config["mqtt_topic"])


def on_lorawan_event(event):
    event_decoded = json.loads(event)

    event_attributes = {
        "id": event_decoded["id"],
        "name": event_decoded["name"],
    }

    lorawan_events_total.add(1, attributes=event_attributes)

    if event_decoded["payload"]:
        on_lorawan_payload(event_decoded)


def on_lorawan_payload(event_decoded):
    on_lht65_payload(event_decoded)


def on_lht65_payload(event_decoded):
    binary_payload = base64.b64decode(event_decoded["payload"])

    event_attributes = {
        "id": event_decoded["id"],
        "name": event_decoded["name"],
    }

    lorawan_battery_voltage_volts.set(
        get_lht65_battery_voltage_volts(binary_payload), attributes=event_attributes
    )

    lorawan_temperature_celsius.set(
        get_lht65_temperature_celsius(binary_payload), attributes=event_attributes
    )

    lorawan_humidity_percentage.set(
        get_lht65_humidity_percentage(binary_payload), attributes=event_attributes
    )

    if event_decoded["hotspots"][0]:
        lorawan_rssi_decibel_milliwatts.set(
            event_decoded["hotspots"][0]["rssi"], attributes=event_attributes
        )
        lorawan_snr_decibels.set(
            event_decoded["hotspots"][0]["snr"], attributes=event_attributes
        )


def get_lht65_temperature_celsius(binary_payload) -> float:
    return int.from_bytes(binary_payload[2:4], byteorder="big") / 100


def get_lht65_humidity_percentage(binary_payload) -> float:
    return int.from_bytes(binary_payload[4:6], byteorder="big") / 10


def get_lht65_battery_voltage_volts(binary_payload) -> float:
    return (int.from_bytes(binary_payload[:2], byteorder="big") & 0x3FFF) / 1000


def on_message(client, userdata, msg):
    try:
        event = msg.payload.decode()
        logger.debug(f"Received UTF-8 payload `{event}` from `{msg.topic}` topic")
        on_lorawan_event(event)
    except Exception as e:
        logger.warning(f"Exception occurred in on_message: {e}")


if __name__ == "__main__":
    mqttc = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)

    mqttc.on_connect = on_connect
    mqttc.on_message = on_message

    if int(config["mqtt_use_tls"]) == 1:
        tlscontext = ssl.create_default_context()
        mqttc.tls_set_context(tlscontext)

    if config["mqtt_username"]:
        mqttc.username_pw_set(config["mqtt_username"], config["mqtt_password"])

    mqttc.connect(
        config["mqtt_address"],
        int(config["mqtt_port"]),
        keepalive=int(config["mqtt_keepalive"]),
    )

    mqttc.loop_forever()
