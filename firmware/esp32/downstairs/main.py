import time

from machine import I2C, Pin, reset
from network import WLAN
from urequests import get

i2c = {
    "1": I2C(scl=Pin(22), sda=Pin(21), freq=100000),
    "2": I2C(scl=Pin(26), sda=Pin(25), freq=100000)
}


DEFAULTS = {
    "1": {
        "red": 0,
        "yellow": 0,
        "green": 0,
        "blue": 0
    },
    "2": {
        "red": 0,
        "yellow": 0,
        "green": 0,
        "blue": 1
    }
}

OTHER = {
    "1": {
        "red": 0,
        "yellow": 1,
        "green": 1,
        "blue": 0,
    },
    "2": {
        "red": 1,
        "yellow": 0,
        "green": 0,
        "blue": 1
    }
}


def main_loop(i2c):
    error_count = 0
    wlan = WLAN()

    while True:
        if not wlan.isconnected():
            error_count += 1
            if error_count > 9:
                reset()
            set_relays(DEFAULTS, i2c)
        elif not i2c["1"].scan():
            reset()
        else:
            error_count = 0
            try:
                set_relays(get("http://192.168.1.192/downstairs.json").json(), i2c)
            except:
                set_relays(DEFAULTS, i2c)
        time.sleep(15)


def set_relays(data, i2c):
    mapping = {
        'blue': 4,
        'red': 1,
        'green': 2,
        'yellow': 8,
    }

    for key in data:
        if key not in DEFAULTS:
            continue
        message = sum([int(data[key].get(color, False)) * value for color, value in mapping.items()])
        i2c[key].writeto(0x11, bytes([0x10, message]))


set_relays(DEFAULTS, i2c)
main_loop(i2c)
