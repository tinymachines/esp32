use embedded_graphics::{
    mono_font::{ascii::FONT_6X10, MonoTextStyleBuilder},
    pixelcolor::BinaryColor,
    prelude::*,
    text::Text,
};
use esp_idf_svc::eventloop::EspSystemEventLoop;
use esp_idf_svc::hal::i2c::{I2cConfig, I2cDriver};
use esp_idf_svc::hal::peripherals::Peripherals;
use esp_idf_svc::nvs::EspDefaultNvsPartition;
use esp_idf_svc::wifi::{AuthMethod, BlockingWifi, ClientConfiguration, Configuration, EspWifi};
use smart_leds::hsv::{hsv2rgb, Hsv};
use smart_leds::SmartLedsWrite;
use ssd1306::{prelude::*, I2CDisplayInterface, Ssd1306};
use std::thread;
use std::time::Duration;
use ws2812_esp32_rmt_driver::Ws2812Esp32Rmt;

const WIFI_SSID: &str = env!("WIFI_SSID");
const WIFI_PASS: &str = env!("WIFI_PASS");

fn main() -> anyhow::Result<()> {
    esp_idf_svc::sys::link_patches();
    esp_idf_svc::log::EspLogger::initialize_default();

    let peripherals = Peripherals::take()?;

    // LED setup (first so we get visual feedback immediately)
    let mut ws2812 = Ws2812Esp32Rmt::new(peripherals.rmt.channel0, peripherals.pins.gpio8)?;
    log::info!("RGB LED ready!");

    // OLED display setup (SSD1306 128x64 I2C on GPIO6/GPIO7)
    let i2c_config = I2cConfig::new().baudrate(400_000.into());
    let i2c = I2cDriver::new(
        peripherals.i2c0,
        peripherals.pins.gpio6,
        peripherals.pins.gpio7,
        &i2c_config,
    )?;

    let interface = I2CDisplayInterface::new(i2c);
    let mut display = Ssd1306::new(interface, DisplaySize128x64, DisplayRotation::Rotate0)
        .into_buffered_graphics_mode();
    display
        .init()
        .map_err(|e| anyhow::anyhow!("Display init: {:?}", e))?;
    display.clear_buffer();

    let text_style = MonoTextStyleBuilder::new()
        .font(&FONT_6X10)
        .text_color(BinaryColor::On)
        .build();

    Text::new("Hello from ESP32!", Point::new(0, 10), text_style)
        .draw(&mut display)
        .map_err(|e| anyhow::anyhow!("Draw: {:?}", e))?;

    // WiFi setup (non-fatal — display and LED work regardless)
    let sysloop = EspSystemEventLoop::take()?;
    let nvs = EspDefaultNvsPartition::take()?;
    let mut wifi = BlockingWifi::wrap(
        EspWifi::new(peripherals.modem, sysloop.clone(), Some(nvs))?,
        sysloop,
    )?;

    let wifi_ip = (|| -> anyhow::Result<String> {
        wifi.set_configuration(&Configuration::Client(ClientConfiguration {
            ssid: WIFI_SSID.try_into().map_err(|_| anyhow::anyhow!("SSID too long"))?,
            password: WIFI_PASS.try_into().map_err(|_| anyhow::anyhow!("Password too long"))?,
            auth_method: AuthMethod::WPA2Personal,
            ..Default::default()
        }))?;

        wifi.start()?;
        log::info!("WiFi started");

        wifi.connect()?;
        log::info!("WiFi connected");

        wifi.wait_netif_up()?;
        let ip_info = wifi.wifi().sta_netif().get_ip_info()?;
        log::info!("WiFi DHCP info: {:?}", ip_info);

        Ok(format!("IP: {}", ip_info.ip))
    })();

    let status_line = match &wifi_ip {
        Ok(ip) => ip.as_str(),
        Err(e) => {
            log::error!("WiFi failed: {}", e);
            "WiFi: offline"
        }
    };

    Text::new(status_line, Point::new(0, 24), text_style)
        .draw(&mut display)
        .map_err(|e| anyhow::anyhow!("Draw: {:?}", e))?;

    display
        .flush()
        .map_err(|e| anyhow::anyhow!("Flush: {:?}", e))?;
    log::info!("OLED display initialized!");

    let mut hue: u8 = 0;
    loop {
        let color = hsv2rgb(Hsv {
            hue,
            sat: 255,
            val: 20,
        });
        ws2812.write([color].iter().copied())?;
        log::info!("Hue: {}", hue);

        hue = hue.wrapping_add(5);
        thread::sleep(Duration::from_millis(100));
    }
}
