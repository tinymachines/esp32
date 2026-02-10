#!/bin/bash

# 1. Install Rust
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
source ~/.cargo/env

# 2. System deps (Raspberry Pi OS is Debian-based)
sudo apt install -y git curl gcc build-essential pkg-config \
    libudev-dev libssl-dev python3 python3-pip python3-venv cmake ninja-build

# 3. Install espup (use the aarch64 binary)
cargo install espup --locked
espup install --targets esp32c6
source ~/export-esp.sh

# 4. If espup's RISC-V target add fails, do it manually:
rustup target add riscv32imac-unknown-none-elf --toolchain nightly

# 5. Install flash tools
cargo install cargo-espflash ldproxy

# 6. Serial permissions
sudo usermod -aG dialout $USER
# Log out and back in

# 7. Optional: add swap if you have 4 GB RAM
#sudo fallocate -l 4G /swapfile
#sudo chmod 600 /swapfile
#sudo mkswap /swapfile
#sudo swapon /swapfile
