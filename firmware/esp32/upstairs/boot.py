import gc
import time

import esp
import network
from wifi_config import WIFI_PASSWORD, WIFI_SSID

esp.osdebug(0)

gc.collect()

station = network.WLAN(network.STA_IF)

station.active(True)
station.connect(WIFI_SSID, WIFI_PASSWORD)

while station.isconnected() == False:
    time.sleep(0.01)
