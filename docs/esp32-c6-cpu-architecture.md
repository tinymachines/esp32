# ESP32-C6 RISC-V CPU Architecture

> **RV32IMAC · 32-bit · Little-Endian · Load-Store Architecture**
>
> Deep dive into the instruction set, register file, memory addressing, instruction encoding, and assembly language for the ESP32-C6's RISC-V cores.

---

## ISA Overview — RV32IMAC

Both the HP (high-performance, 160 MHz) and LP (low-power, 20 MHz) cores implement the **RV32IMAC** instruction set architecture. This is a modular RISC-V profile built from a base plus three standard extensions. The letters decode as follows:

| Letter | Extension | Description |
|--------|-----------|-------------|
| **RV32** | Base width | 32-bit RISC-V architecture, 32-bit address space |
| **I** | Base Integer | 40 base instructions: ALU ops, loads/stores, branches, jumps. 32 registers, byte-addressable little-endian memory. Fixed 32-bit instruction encoding. |
| **M** | Multiply / Divide | Hardware integer multiplication and division: `MUL`, `MULH`, `MULHU`, `MULHSU`, `DIV`, `DIVU`, `REM`, `REMU` |
| **A** | Atomics | Atomic read-modify-write operations for synchronization: `LR.W`/`SC.W` (load-reserved/store-conditional), `AMOSWAP`, `AMOADD`, `AMOAND`, `AMOOR`, `AMOXOR`, `AMOMIN`, `AMOMAX` |
| **C** | Compressed | 16-bit encodings for the most common instructions. Reduces code size by ~25–30%. Uses a subset of registers (x8–x15) and smaller immediate ranges. Instructions like `C.LW`, `C.ADDI`, `C.J`, `C.RET`. |

**What's NOT included:** The ESP32-C6 does *not* have the F (single-precision float) or D (double-precision float) extensions. All floating-point math is done in software via the compiler's soft-float library. This is typical for embedded MCUs and means `f32`/`f64` operations in Rust will be slower than integer math.

**Rust target triple:** `riscv32imac-unknown-none-elf`

---

## Register File — 32 × 32-bit GPRs

RISC-V defines 32 general-purpose integer registers (`x0`–`x31`), each 32 bits wide. The ABI (Application Binary Interface) gives them conventional names and roles. There are no floating-point registers on the C6.

| Register | ABI Name | Role | Saved By |
|----------|----------|------|----------|
| `x0` | `zero` | Hardwired zero — reads always return 0, writes are discarded | — |
| `x1` | `ra` | Return address (set by `JAL`/`JALR`) | Caller |
| `x2` | `sp` | Stack pointer | Callee |
| `x3` | `gp` | Global pointer (linker-relaxation base) | — |
| `x4` | `tp` | Thread pointer | — |
| `x5–x7` | `t0–t2` | Temporary registers | Caller |
| `x8` | `s0` / `fp` | Saved register / frame pointer | Callee |
| `x9` | `s1` | Saved register | Callee |
| `x10–x11` | `a0–a1` | Function arguments + return values | Caller |
| `x12–x17` | `a2–a7` | Function arguments | Caller |
| `x18–x27` | `s2–s11` | Saved registers | Callee |
| `x28–x31` | `t3–t6` | Temporary registers | Caller |

Additional non-GPR state:

| Register | Purpose |
|----------|---------|
| `pc` | Program counter — holds address of current instruction (not directly accessible as a GPR) |
| CSRs | Control/Status Registers — `mstatus`, `mtvec`, `mcause`, `mepc`, `mie`, `mip`, `mcycle`, `minstret`, etc. |

**Caller vs Callee saved:** "Caller saved" means the calling function must save these registers before a function call if it needs them afterwards. "Callee saved" means the called function must preserve and restore them. This is a convention enforced by the compiler — the hardware doesn't care.

---

## Instruction Encoding Formats

RV32I defines six core instruction formats. All are exactly 32 bits wide (the C extension adds 16-bit variants). Source and destination register fields are always in the same bit positions across formats to simplify hardware decoding. The sign bit of every immediate is always in bit 31.

### R-Type — Register-to-Register

Used for ALU operations between two source registers. Example: `ADD rd, rs1, rs2`

```
| funct7  | rs2   | rs1   | funct3 | rd    | opcode |
| 31:25   | 24:20 | 19:15 | 14:12  | 11:7  | 6:0    |
| 7 bits  | 5 bits| 5 bits| 3 bits | 5 bits| 7 bits |
```

### I-Type — Immediate

Used for loads, ALU with immediate, `JALR`. 12-bit sign-extended immediate. Example: `ADDI rd, rs1, imm`

```
| imm[11:0]       | rs1   | funct3 | rd    | opcode |
| 31:20           | 19:15 | 14:12  | 11:7  | 6:0    |
| 12 bits         | 5 bits| 3 bits | 5 bits| 7 bits |
```

### S-Type — Store

Used for storing register data to memory. Immediate split across two fields. Example: `SW rs2, offset(rs1)`

```
| imm[11:5] | rs2   | rs1   | funct3 | imm[4:0] | opcode |
| 31:25     | 24:20 | 19:15 | 14:12  | 11:7     | 6:0    |
| 7 bits    | 5 bits| 5 bits| 3 bits | 5 bits   | 7 bits |
```

### B-Type — Branch

Conditional branches. PC-relative, 12-bit immediate shifted left by 1 (±4 KiB range). Example: `BEQ rs1, rs2, label`

```
| imm[12|10:5] | rs2   | rs1   | funct3 | imm[4:1|11] | opcode |
| 31:25        | 24:20 | 19:15 | 14:12  | 11:7        | 6:0    |
| 7 bits       | 5 bits| 5 bits| 3 bits | 5 bits      | 7 bits |
```

### U-Type — Upper Immediate

Loads a 20-bit immediate into upper bits of a register. Used by `LUI` and `AUIPC`.

```
| imm[31:12]                          | rd    | opcode |
| 31:12                               | 11:7  | 6:0    |
| 20 bits                             | 5 bits| 7 bits |
```

### J-Type — Jump

Unconditional jump (`JAL`). 20-bit PC-relative immediate shifted left by 1 (±1 MiB range).

```
| imm[20|10:1|11|19:12]              | rd    | opcode |
| 31:12                               | 11:7  | 6:0    |
| 20 bits                             | 5 bits| 7 bits |
```

---

## Memory Addressing

RISC-V is a strict **load-store architecture**: only load and store instructions access memory. All arithmetic operates exclusively on registers. The ESP32-C6 uses a 32-bit address space (4 GiB theoretical) with little-endian byte ordering.

### Addressing Modes

| Mode | Syntax | Address Calculation | Range |
|------|--------|-------------------|-------|
| Base + Offset | `LW rd, imm(rs1)` | addr = rs1 + sign_extend(imm12) | ±2 KiB from base register |
| PC-Relative (branch) | `BEQ rs1, rs2, label` | addr = PC + sign_extend(imm13) | ±4 KiB from PC |
| PC-Relative (jump) | `JAL rd, label` | addr = PC + sign_extend(imm21) | ±1 MiB from PC |
| Indirect (jump) | `JALR rd, rs1, imm` | addr = (rs1 + sign_extend(imm12)) & ~1 | Full 32-bit space |
| Upper Immediate | `LUI rd, imm20` | rd = imm20 << 12 | Upper 20 bits of 32-bit constant |
| PC + Upper Immediate | `AUIPC rd, imm20` | rd = PC + (imm20 << 12) | ±2 GiB from PC |

### Building a Full 32-bit Address

Since immediates max out at 20 bits, loading a full 32-bit address or constant takes two instructions:

```asm
# Load the 32-bit address 0x4080_1234 into register t0
LUI   t0, 0x40802       # t0 = 0x40802000 (upper 20 bits, rounded for sign extension)
ADDI  t0, t0, 0x234     # t0 = 0x40801234 (add lower 12 bits)
```

### Load and Store Widths

| Instruction | Width | Sign Extension |
|-------------|-------|----------------|
| `LB` / `SB` | 8-bit (byte) | Sign-extended on load |
| `LBU` | 8-bit (byte) | Zero-extended |
| `LH` / `SH` | 16-bit (halfword) | Sign-extended on load |
| `LHU` | 16-bit (halfword) | Zero-extended |
| `LW` / `SW` | 32-bit (word) | Full register width |

**Alignment:** The base RISC-V spec allows misaligned loads/stores, but they may trap or be slow. On the ESP32-C6, aligned accesses are fastest — 32-bit loads/stores should be word-aligned (address divisible by 4), 16-bit by 2.

---

## ESP32-C6 Address Space Map

The ESP32-C6 uses a **Harvard-like** bus architecture internally (separate instruction and data buses) but presents a unified 32-bit address space to the programmer. External flash is accessed through an MMU cache with 64 KB page granularity.

| Address Range | Region | Size | Description |
|---------------|--------|------|-------------|
| `0x4000_0000` – `0x407F_FFFF` | IROM | 8 MB | Flash mapped for instruction execution (code runs from here via cache) |
| `0x4080_0000` – `0x4087_FFFF` | IRAM | 512 KB | Internal SRAM — executable, used for ISR handlers and hot code |
| `0x4200_0000` – `0x427F_FFFF` | DROM | 8 MB | Flash mapped for read-only data (constants, string literals) |
| `0x4080_0000` – `0x4087_FFFF` | DRAM | 512 KB (shared) | Internal SRAM — data, heap, stack (shares physical memory with IRAM) |
| `0x6000_0000` – `0x600F_FFFF` | Peripherals | ~1 MB | Memory-mapped I/O — GPIO, SPI, UART, timers, etc. (4 KB per peripheral block) |
| `0x5000_0000` – `0x5000_3FFF` | LP SRAM | 16 KB | Low-power memory — survives deep sleep, accessible by LP core |
| `0x600B_0800` – `0x600B_0FFF` | eFuse | 4096 bits | One-time-programmable configuration and encryption bits |

**IRAM/DRAM sharing:** The internal 512 KB SRAM is mapped to both instruction and data buses at the same physical address range. The linker splits it — code that must be in RAM (ISR handlers, hot functions marked with `IRAM_ATTR`) goes in the IRAM section, and everything else (heap, stack, `.data`, `.bss`) goes into DRAM. More IRAM usage means less DRAM and vice versa.

**Peripheral access:** All peripheral registers are memory-mapped starting at `0x6000_0000`. In Rust, the HAL crates abstract this, but at the bare metal level you read/write these addresses with volatile pointer operations.

**Flash access via cache:** Most application code runs from flash (IROM) through the instruction cache. The MMU maps 64 KB pages of external SPI flash into the CPU's address space. Cache hits are as fast as SRAM; cache misses incur SPI flash latency. Performance-critical functions can be placed in IRAM to avoid cache miss penalties.

---

## RISC-V Assembly on the ESP32-C6

The ESP32-C6 uses standard RISC-V GNU assembler syntax. Below are examples of what the compiler generates and what you might write in inline assembly from Rust.

### Basic ALU and Control Flow

```asm
# Simple loop: sum integers 1 to 10
      li    a0, 0           # a0 = sum = 0 (pseudo: expands to ADDI a0, zero, 0)
      li    a1, 1           # a1 = counter = 1
      li    a2, 11          # a2 = limit = 11
loop:
      add   a0, a0, a1     # sum += counter        (R-type)
      addi  a1, a1, 1      # counter++              (I-type)
      blt   a1, a2, loop   # if counter < limit, branch (B-type)
      ret                   # return (pseudo: JALR zero, ra, 0)
```

### Memory Access

```asm
# Read a 32-bit peripheral register (GPIO output register)
      lui   t0, 0x60004     # t0 = 0x60004000 (GPIO base)    (U-type)
      lw    t1, 0x04(t0)    # t1 = *(t0 + 4)  read GPIO_OUT  (I-type load)

# Write a value to memory
      li    t2, 0xFF         # value to write
      sw    t2, 0x08(t0)    # *(t0 + 8) = t2  (S-type store)

# Byte-level access (reading a sensor byte)
      lbu   a0, 0(a1)       # load unsigned byte from address in a1
```

### Function Call Convention

```asm
# Calling a function: args in a0-a7, return in a0/a1
my_func:
      addi  sp, sp, -16     # allocate 16 bytes on stack
      sw    ra, 12(sp)      # save return address
      sw    s0, 8(sp)       # save callee-saved register

      mv    s0, a0          # preserve arg across call (pseudo: ADDI s0, a0, 0)
      jal   ra, other_fn    # call other_fn (J-type, saves PC+4 in ra)

      add   a0, a0, s0     # combine return value with saved arg
      lw    s0, 8(sp)       # restore s0
      lw    ra, 12(sp)      # restore return address
      addi  sp, sp, 16      # deallocate stack
      ret                   # return to caller
```

### Atomic Operations (A Extension)

```asm
# Atomic increment of a shared counter at address in a0
retry:
      lr.w  t0, (a0)        # load-reserved: t0 = *a0, set reservation
      addi  t0, t0, 1       # increment
      sc.w  t1, t0, (a0)    # store-conditional: *a0 = t0 if reservation held
      bnez  t1, retry       # t1 != 0 means SC failed, retry
```

### Compressed Instructions (C Extension)

```asm
# These 16-bit compressed instructions are encoded by the assembler
# automatically when possible. Same semantics, half the size:
      c.li   a0, 5          # 16-bit: load immediate 5 into a0
      c.addi sp, -16        # 16-bit: adjust stack pointer
      c.lw   a1, 0(a0)      # 16-bit: load word (registers x8-x15 only)
      c.sw   a1, 4(a0)      # 16-bit: store word
      c.j    loop            # 16-bit: unconditional jump
      c.ret                  # 16-bit: return
```

### Inline Assembly in Rust

```rust
use core::arch::asm;

// Read the machine hart ID
let hart_id: u32;
unsafe { asm!("csrr {}, mhartid", out(reg) hart_id) };

// Read the cycle counter
let cycles: u32;
unsafe { asm!("csrr {}, mcycle", out(reg) cycles) };

// Memory fence (full barrier)
unsafe { asm!("fence") };
```

---

## Common Pseudo-Instructions

The assembler provides "pseudo-instructions" that expand into one or more real instructions. These make assembly more readable without adding hardware complexity.

| Pseudo | Expands To | Purpose |
|--------|-----------|---------|
| `NOP` | `ADDI x0, x0, 0` | No operation |
| `LI rd, imm` | `LUI` + `ADDI` | Load arbitrary 32-bit immediate |
| `LA rd, symbol` | `AUIPC` + `ADDI` | Load address of symbol |
| `MV rd, rs` | `ADDI rd, rs, 0` | Register-to-register move |
| `NOT rd, rs` | `XORI rd, rs, -1` | Bitwise NOT |
| `NEG rd, rs` | `SUB rd, x0, rs` | Negate (two's complement) |
| `J offset` | `JAL x0, offset` | Unconditional jump (discard return addr) |
| `JR rs` | `JALR x0, rs, 0` | Jump to register (indirect) |
| `RET` | `JALR x0, ra, 0` | Return from function |
| `CALL fn` | `AUIPC ra` + `JALR ra` | Far function call |
| `BEQZ rs, off` | `BEQ rs, x0, off` | Branch if zero |
| `BNEZ rs, off` | `BNE rs, x0, off` | Branch if not zero |
| `SEQZ rd, rs` | `SLTIU rd, rs, 1` | Set if equal to zero |
| `SNEZ rd, rs` | `SLTU rd, x0, rs` | Set if not equal to zero |

RISC-V deliberately keeps the hardware instruction set minimal. Instead of `MOV`, `NOP`, or `NOT` instructions in silicon, the assembler synthesizes them from existing ops. This keeps the decoder simple and the chip small.

---

## Privilege Levels and Control/Status Registers

The ESP32-C6 HP core implements RISC-V Machine mode (M-mode) and optionally User mode. When running under ESP-IDF/FreeRTOS, the kernel runs in M-mode. Tasks can run in U-mode when the Trusted Execution Environment (TEE) is enabled.

### Privilege Modes

| Mode | Level | Access | Use Case |
|------|-------|--------|----------|
| M-mode (Machine) | Highest | Full hardware access, all CSRs, interrupt handling | RTOS kernel, bootloader, bare-metal firmware |
| U-mode (User) | Lowest | Restricted — no direct CSR/peripheral access | Application tasks (with TEE enabled) |

### Key Control/Status Registers

| CSR | Purpose |
|-----|---------|
| `mstatus` | Global interrupt enable, privilege mode stack, previous privilege level |
| `mtvec` | Trap vector base address — where the CPU jumps on exception/interrupt |
| `mcause` | Cause of the most recent exception or interrupt (encoded as interrupt ID) |
| `mepc` | Exception program counter — return address after trap handling |
| `mie` | Machine interrupt enable — per-source interrupt enable bits |
| `mip` | Machine interrupt pending — which interrupts are currently pending |
| `mcycle` | Machine cycle counter (performance monitoring) |
| `minstret` | Machine instructions-retired counter (performance monitoring) |
| `mscratch` | Scratch register for M-mode trap handlers |
| `mtval` | Trap value — additional info about the trap (e.g., faulting address) |

### CSR Access Instructions

| Instruction | Operation |
|-------------|-----------|
| `CSRRW rd, csr, rs1` | Atomic read CSR into rd, write rs1 into CSR |
| `CSRRS rd, csr, rs1` | Read CSR into rd, set bits specified by rs1 |
| `CSRRC rd, csr, rs1` | Read CSR into rd, clear bits specified by rs1 |
| `CSRRWI rd, csr, imm5` | Same as CSRRW but with 5-bit zero-extended immediate |
| `CSRRSI rd, csr, imm5` | Same as CSRRS but with 5-bit immediate |
| `CSRRCI rd, csr, imm5` | Same as CSRRC but with 5-bit immediate |

### Debug Support

The ESP32-C6 includes a RISC-V Debug Module (v0.13 compliant) accessible via JTAG over the USB interface. This supports hardware breakpoints (up to 4 triggers), single-stepping, and a trace encoder for instruction tracing — all usable from OpenOCD and `espflash`.

---

## Rust Toolchain Mapping

How the ISA maps to the Rust compilation target and what it means for your code.

| Property | Value |
|----------|-------|
| Rust target triple | `riscv32imac-unknown-none-elf` |
| Architecture | RV32IMAC (32-bit RISC-V with M, A, C extensions) |
| Endianness | Little-endian |
| ABI | `ilp32` (int, long, pointer = 32-bit, soft-float) |
| Float support | Software only (no F/D extensions) |
| Atomic support | Hardware — `core::sync::atomic` works natively via A extension |
| Pointer width | 32-bit (4 bytes) |
| Linker | `rust-lld` (LLVM's LLD linker) |
| Object format | ELF32-littleriscv |
| `#[no_std]` | Required for bare-metal; `std` available with ESP-IDF |

### Useful Commands

```bash
# Disassemble your firmware to see generated RISC-V instructions
cargo objdump --release -- -d

# Show section sizes (text, data, bss)
cargo size --release

# Generate full assembly listing
cargo objdump --release -- -S > listing.asm

# Print the target spec
rustc --print target-spec-json -Z unstable-options --target riscv32imac-unknown-none-elf
```

---

*Sources: RISC-V ISA Specification (v20191213) · ESP32-C6 Technical Reference Manual · ESP32-C6 Datasheet v1.3*
