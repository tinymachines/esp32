# Kit Inventory

**Kit:** LAFVIN Basic Starter Kit for ESP32 ([docs](https://basic-starter-kit-for-esp32-s3-wroom.readthedocs.io/en/latest/index.html))

**Board swap:** The kit ships with an ESP32-S3-WROOM. We're using an **ESP32-C6-DevKitC-1-N8** instead. Pin numbers and peripherals differ, but the kit components work the same.

## Status

| Component | Status | GPIO / Notes |
|-----------|--------|-------------|
| WS2812 onboard LED | Online | GPIO8 (RMT peripheral) |
| SSD1306 0.96" OLED | Online | GPIO6 (SDA), GPIO7 (SCL), I2C @ 400 kHz |
| WiFi | Online | Station mode, WPA2Personal |
| DHT11 temp/humidity | Not yet | |
| HC-SR501 PIR motion | Not yet | |
| Photoresistor module | Not yet | |
| Potentiometer (10K) | Not yet | |
| Relay module (2-ch) | Not yet | |
| Active buzzer | Not yet | |
| Passive buzzer | Not yet | |
| LEDs (red/yellow/green) | Not yet | |
| RGB LEDs (4-pin) | Not yet | |
| Buttons | Not yet | |

## Full Parts List

| Component | Qty | Description |
|-----------|-----|-------------|
| ESP32-C6-DevKitC-1-N8 | 1 | RISC-V, WiFi 6, BLE 5, 8 MB flash (our board, not from kit) |
| 0.96" SSD1306 OLED | 1 | 128x64 monochrome, I2C (addr 0x3C) |
| 830-point breadboard | 1 | Full-size solderless breadboard |
| DHT11 sensor | 1 | Temperature (0-50C, ±2C) and humidity (20-80%, ±5%) — digital single-wire protocol |
| HC-SR501 PIR sensor | 1 | Passive infrared motion detector, ~7m range, adjustable delay and sensitivity |
| Photoresistor module | 1 | Light-dependent resistor, read via ADC for ambient light level |
| Potentiometer | 1 | 10KΩ rotary, analog input via ADC |
| 5V 2-channel relay | 1 | Optocoupler-isolated, switch AC/DC loads up to 10A 250VAC |
| Active buzzer | 1 | Internal oscillator — just apply DC voltage to sound |
| Passive buzzer | 1 | No internal oscillator — drive with PWM to play specific frequencies/tones |
| Red LEDs | 5 | Standard 5mm, ~2V forward voltage |
| Yellow LEDs | 5 | Standard 5mm, ~2V forward voltage |
| Green LEDs | 5 | Standard 5mm, ~2V forward voltage |
| RGB LEDs | 2 | Common cathode, 4 pins (R, G, B, GND), mix colors with PWM |
| Tactile buttons | 6 | Momentary pushbuttons, normally open |
| Resistors (220Ω) | 10 | Current limiting for LEDs |
| Resistors (1KΩ) | 10 | General purpose / voltage dividers |
| Resistors (10KΩ) | 10 | Pull-up/pull-down resistors |
| Dupont wires (M-M) | 10 | Male-to-male jumper wires |
| Dupont wires (F-M) | 10 | Female-to-male jumper wires |
| Dupont wires (F-F) | 10 | Female-to-female jumper wires |
| USB cable | 1 | For power and serial communication |

## Component Notes

### DHT11 (likely next peripheral)
- Single-wire digital protocol (not I2C or SPI)
- Needs a 10KΩ pull-up resistor on the data line
- Slow: minimum 1 second between reads
- Good for ambient monitoring — display temp/humidity on the OLED

### HC-SR501 PIR Sensor
- Digital output: HIGH when motion detected, LOW otherwise
- Two trim pots on the board: sensitivity and hold time
- Needs 1-minute warm-up after power-on
- 3.3V compatible output, but the module itself wants 5V power (can sometimes work at 3.3V)

### Photoresistor
- Analog output: resistance drops as light increases
- Read via ESP32 ADC — gives a 0-4095 value proportional to light level
- Useful for auto-brightness on the OLED

### Passive vs Active Buzzer
- **Active**: has a built-in oscillator. Apply voltage → it beeps at a fixed frequency. Simple GPIO on/off.
- **Passive**: no oscillator. You provide a square wave via PWM. Control the frequency = control the pitch. Can play melodies.
