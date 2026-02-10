use esp_idf_svc::eventloop::EspSystemEventLoop;
use esp_idf_svc::hal::peripherals::Peripherals;
use esp_idf_svc::nvs::EspDefaultNvsPartition;
use esp_idf_svc::wifi::{AuthMethod, BlockingWifi, ClientConfiguration, Configuration, EspWifi};
use smart_leds::hsv::{hsv2rgb, Hsv};
use smart_leds::SmartLedsWrite;
use std::thread;
use std::time::Duration;
use ws2812_esp32_rmt_driver::Ws2812Esp32Rmt;

const WIFI_SSID: &str = env!("WIFI_SSID");
const WIFI_PASS: &str = env!("WIFI_PASS");

fn main() -> anyhow::Result<()> {
    esp_idf_svc::sys::link_patches();
    esp_idf_svc::log::EspLogger::initialize_default();

    let peripherals = Peripherals::take()?;
    let sysloop = EspSystemEventLoop::take()?;
    let nvs = EspDefaultNvsPartition::take()?;

    // WiFi setup
    let mut wifi = BlockingWifi::wrap(EspWifi::new(peripherals.modem, sysloop.clone(), Some(nvs))?, sysloop)?;

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

    // LED setup
    let mut ws2812 = Ws2812Esp32Rmt::new(peripherals.rmt.channel0, peripherals.pins.gpio8)?;

    log::info!("RGB LED ready!");

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
