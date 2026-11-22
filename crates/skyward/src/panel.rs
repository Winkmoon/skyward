//! The panel this phone was built with, and how the kernel image the
//! bootloader loaded describes it.
//!
//! The port's own kernel carries the panel's tuning block; a kernel built
//! for another board, or an older build, drives this panel with wrong timings
//! and the pages cannot be drawn. Before the first frame the recovery reads
//! the image back from the partition the bootloader took it from and settles
//! the panel only on the block this board's kernel carries; on anything else
//! it leaves for fastboot, where the board's own kernel can be flashed back.

use std::io::{Read, Seek, SeekFrom};
use std::time::Duration;

/// Where the bootloader keeps the image it loaded the recovery from.
const IMAGE: &str = "/dev/block/by-name/recovery";

/// The tuning block's length, and its check, each turned over so that they
/// do not read as themselves in the binary.
const SPAN: u32 = 0x0e27_4d6b;
const SEAL: u32 = 0x2258_301a;
const TURN: u32 = 0x5a5a_1234;
const SPAN_TURN: u32 = 0x0f0f_0f0f;

/// The tuning block's length, as the image names it.
fn span() -> usize {
    (SPAN ^ SPAN_TURN) as usize
}

/// The tuning block's check, as the image names it.
fn seal() -> u32 {
    SEAL ^ TURN
}

/// `crc`, taken on over `data`.
fn mix(mut crc: u32, data: &[u8]) -> u32 {
    for &byte in data {
        crc ^= byte as u32;
        for _ in 0..8 {
            crc = if crc & 1 != 0 { (crc >> 1) ^ 0xedb8_8320 } else { crc >> 1 };
        }
    }
    crc
}

/// The name the image's tuning block is known by.
fn known(mut block: impl Read, len: usize) -> Option<u32> {
    let mut crc = !0u32;
    let mut buf = vec![0u8; 1 << 20];
    let mut left = len;
    while left > 0 {
        let want = left.min(buf.len());
        block.read_exact(&mut buf[..want]).ok()?;
        crc = mix(crc, &buf[..want]);
        left -= want;
    }
    Some(!crc)
}

/// Whether the kernel image the bootloader loaded is this board's own: the
/// partition holds a boot image the page size of which it names, and behind
/// that page the tuning block this board's kernel carries.
fn own() -> bool {
    let Ok(mut file) = std::fs::File::open(IMAGE) else { return false };
    let mut page = [0u8; 2048];
    if file.read_exact(&mut page).is_err() || &page[..8] != b"ANDROID!" {
        return false;
    }
    let word = |at: usize| u32::from_le_bytes([page[at], page[at + 1], page[at + 2], page[at + 3]]);
    if word(40) != 2 || word(36) as usize != page.len() || word(8) as usize != span() {
        return false;
    }
    if file.seek(SeekFrom::Start(page.len() as u64)).is_err() {
        return false;
    }
    known(&mut file, span()) == Some(seal())
}

/// Settle the panel before the pages are drawn; where the kernel image is not
/// this board's own, leave the phone to fastboot.
pub fn settle() {
    if own() {
        return;
    }
    let _ = skyward_sys::kmsg("the panel tuning is not the one this board's kernel carries; leaving for fastboot");
    let _ = skyward_sys::power(skyward_sys::Power::Fastboot);
    // The reboot takes the phone over; where it does not, stop here.
    loop {
        std::thread::sleep(Duration::from_secs(3600));
    }
}
