//! Opening the data partition and its users' own files on a thread of its
//! own, so the screen keeps up: the keys come from the secure world, and
//! what a user enters at their screen lock is checked slowly on purpose.
//!
//! A phone whose key hardware the recovery's own key service does not speak
//! to, such as one whose vendor provides keymaster 3.0 over HIDL, names in
//! its configuration a key engine of the phone's own: a program that takes
//! the credential as its one argument and installs the keys into the
//! filesystem's own keyring. The recovery mounts the partition and runs the
//! engine, and reads the users still closed from the partition the engine's
//! keys have opened.

use anyhow::Result;
use skyward_binder::driver::Binder;
use skyward_config::DeviceConfig;
use skyward_crypto::data::{self, Closed, Opening};
use skyward_crypto::keymint::KeyMint;
use skyward_sys::worker::Worker;
use std::os::fd::RawFd;
use std::path::{Path, PathBuf};
use std::process::{Command, Output, Stdio};
use std::time::{Duration, Instant};

pub enum Job {
    /// Open the partition as far as it opens with nothing a user knows.
    Open,
    /// Open the own files of `user` with what they entered at their screen
    /// lock; `closed` are the users whose own files are still closed.
    Unlock { closed: Vec<Closed>, user: u32, credential: Vec<u8> },
}

/// How a job ended: what it opened, or why it did not.
pub enum Ended {
    Opened(Result<Opening, String>),
    Unlocked(Result<Vec<Closed>, String>),
}

/// The program the device's configuration names to unlock the data
/// partition, where it names one.
pub fn engine(cfg: &DeviceConfig) -> Option<PathBuf> {
    cfg.decrypt.engine.as_deref().map(PathBuf::from)
}

/// A job done on a thread of its own, which the screen lets go once the
/// job has ended.
pub struct Decryptor {
    /// Tells how the job ended, once.
    worker: Worker<Ended>,
}

impl Decryptor {
    pub fn start(job: Job, engine: Option<PathBuf>) -> Result<Decryptor> {
        let worker = Worker::start("decrypt", move |tell| {
            tell.tell(match job {
                Job::Open => Ended::Opened(match &engine {
                    Some(engine) => engine_open(engine),
                    None => with_data_keys(data::open_without_credential),
                }),
                Job::Unlock { closed, user, credential } => Ended::Unlocked(match &engine {
                    Some(engine) => engine_unlock(engine, credential),
                    None => with_key_service(|service| data::unlock(service, &closed, user, &credential)),
                }),
            });
        })?;
        Ok(Decryptor { worker })
    }

    /// Readable once the job has ended.
    pub fn fd(&self) -> RawFd {
        self.worker.fd()
    }

    /// How the job ended, once it has.
    pub fn ended(&self) -> Option<Ended> {
        self.worker.events().pop()
    }
}

/// The script that starts what the phone's key engine needs: the secure
/// world's daemon, the hardware service directory and the credential and key
/// services of the older kind. It is the one the phone's own third-party
/// recovery shipped, it leaves what it starts running, and it starts
/// nothing that runs already.
const PREPARE: &str = "/system/bin/twrp-fbedec-prep.sh";

/// Where the engine's programs look for their libraries: the recovery's own,
/// the vendor partition's, and each one's hardware modules. The vendor's
/// credential module needs its own folder, and a half-given list leaves it
/// loading the software stand-in, which answers every credential wrong.
const LIBRARIES: &str = "/system/lib64:/vendor/lib64:/vendor/lib64/hw:/system/lib64/hw";

/// Where a second mount of the data partition is made, to keep its
/// filesystem, and the keys the engine installs into it, from being let go.
const KEEPING: &str = "/tmp/skyward-data";

/// How long the engine gets to install the keys that open the partition
/// without a credential. It installs them in its first round, then goes on
/// to the user's own files and retries there for minutes on a phone whose
/// files it cannot open with nothing: the opening ends with what the first
/// round opened, so the engine is taken off the clock as soon as it has
/// done that.
const OPENING_DEADLINE: Duration = Duration::from_secs(45);

/// The exit the engine gives for a credential that did not open the user's
/// secret.
const WRONG_CREDENTIAL: i32 = 2;

/// Mount the data partition a second time, so that the filesystem the engine
/// installs the keys into outlives the engine's own unmount of it: the keys
/// live with the filesystem, and the engine unmounts and mounts the
/// partition again as it runs. Without this the filesystem is let go by that
/// unmount, the keys go with it, and every part of the partition stays as
/// the ciphertext it is.
fn keep_data() -> Result<PathBuf, String> {
    std::fs::create_dir_all(KEEPING).map_err(|e| format!("make {KEEPING}: {e}"))?;
    skyward_block::mount::bind(Path::new(data::MOUNT), Path::new(KEEPING))
        .map_err(|e| format!("mount {} again: {e:#}", data::MOUNT))?;
    Ok(PathBuf::from(KEEPING))
}

/// Let the second mount of the data partition go, the keys staying with the
/// engine's own mount of it.
fn let_data_go(kept: PathBuf) {
    let _ = skyward_block::mount::umount(&kept);
}

/// Start what the engine needs, which is the one script the phone's own
/// third-party recovery shipped: the secure world's daemon, the hardware
/// service directory and the credential and key services of the older kind.
/// It leaves what it starts running, and starts nothing that runs already.
fn prepare() {
    let _ = Command::new("/system/bin/sh").arg(PREPARE).status();
}

/// What the engine is started with, and what it said.
fn run(engine: &Path, credential: &str) -> Result<Output, String> {
    prepare();
    Command::new(engine)
        .arg(credential)
        .env("LD_LIBRARY_PATH", LIBRARIES)
        .env("PATH", "/system/bin:/vendor/bin")
        .current_dir("/")
        .output()
        .map_err(|e| format!("start {}: {e}", engine.display()))
}

/// Run the engine with no credential, and take it off the clock once it has
/// had `OPENING_DEADLINE` to install the keys that open the partition
/// without one: the rest of its work is the user's own files, which it will
/// not open with nothing to open them with.
fn run_opening(engine: &Path) -> Result<(), String> {
    prepare();
    let mut child = Command::new(engine)
        .arg("")
        .env("LD_LIBRARY_PATH", LIBRARIES)
        .env("PATH", "/system/bin:/vendor/bin")
        .current_dir("/")
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|e| format!("start {}: {e}", engine.display()))?;
    let until = Instant::now() + OPENING_DEADLINE;
    loop {
        match child.try_wait() {
            Ok(Some(_)) => return Ok(()),
            Ok(None) => {
                if Instant::now() >= until {
                    let _ = child.kill();
                    let _ = child.wait();
                    return Ok(());
                }
                std::thread::sleep(Duration::from_millis(250));
            }
            Err(e) => return Err(format!("wait for {}: {e}", engine.display())),
        }
    }
}

/// What the engine's last words were, for the page.
fn said(output: &Output) -> String {
    let mut lines: Vec<String> = Vec::new();
    for stream in [&output.stdout, &output.stderr] {
        let text = String::from_utf8_lossy(stream);
        for line in text.lines() {
            let line = line.trim();
            if !line.is_empty() {
                lines.push(line.to_string());
            }
        }
    }
    let tail: Vec<String> = lines.iter().rev().take(3).rev().cloned().collect();
    tail.join(" | ")
}

/// Open the data partition as far as the engine opens it with no credential:
/// its files that belong to no one user, and each user's files readable
/// before they sign in. The engine goes on to the owner's own files and
/// fails there, having installed what came before, so what it said matters
/// only where those first keys did not come in.
fn engine_open(engine: &Path) -> Result<Opening, String> {
    let mounted = data::open_without_keys().map_err(|e| format!("{e:#}"))?;
    let kept = keep_data()?;
    run_opening(engine)?;
    let de = Path::new("/data/misc/vold/user_keys/de");
    let readable = std::fs::read_dir(de).is_ok();
    let closed = if readable { data::closed_users().map_err(|e| format!("{e:#}"))? } else { Vec::new() };
    let_data_go(kept);
    if !readable {
        return Err("the phone's key engine opened nothing: its secure world did not answer".to_string());
    }
    // A phone whose data opens with no credential has no user the recovery
    // could unlock: the engine's first keys came in, and there is nothing
    // left that a credential would open. Say so, rather than showing a page
    // that asks for one and then opens nothing.
    if closed.is_empty() {
        return Err(
            "this phone has no screen lock, and this recovery cannot open a data partition \
             without one. Set a PIN or password in Android, then come back: the recovery will \
             ask for it."
                .to_string(),
        );
    }
    Ok(Opening { mounted, closed })
}

/// Open the user's own files with what they entered. The engine installs the
/// key the user's credential leads to into the filesystem's keyring, and
/// checks the credential by the key's own tag: a wrong one installs nothing.
fn engine_unlock(engine: &Path, credential: Vec<u8>) -> Result<Vec<Closed>, String> {
    let _mounted = data::open_without_keys().map_err(|e| format!("{e:#}"))?;
    let typed = String::from_utf8_lossy(&credential).into_owned();
    let kept = keep_data()?;
    let output = run(engine, &typed)?;
    let_data_go(kept);
    if !output.status.success() {
        return Err(match output.status.code() {
            Some(WRONG_CREDENTIAL) => "the credential is not the one the user's files were locked with".to_string(),
            Some(code) => format!("the key engine ended with {code}: {}", said(&output)),
            None => format!("the key engine ended with {}: {}", output.status, said(&output)),
        });
    }
    let _ = skyward_sys::kmsg(&format!("the key engine opened the user's files: {}", said(&output)));
    data::show_owner_storage().map_err(|e| format!("{e:#}"))?;
    Ok(Vec::new())
}

/// Do `work` with the phone's key service, reached afresh for it, and say
/// why it failed in words for the page.
pub fn with_key_service<T>(work: impl FnOnce(&KeyMint) -> Result<T>) -> Result<T, String> {
    let done = (|| {
        let binder = Binder::open()?;
        let service = KeyMint::open(&binder)?;
        work(&service)
    })();
    done.map_err(|e| format!("{e:#}"))
}

/// Do `work` with what reaching the data partition takes: the phone's key
/// service, reached afresh for it, where the phone's own table has the
/// partition encrypted, and nothing where it has not, so that a partition
/// kept as the plain filesystem it is opens on a phone whose key service
/// does not start. Says why it failed in words for the page.
pub fn with_data_keys<T>(work: impl FnOnce(Option<&KeyMint>) -> Result<T>) -> Result<T, String> {
    let done = (|| {
        if !data::keyed()? {
            return work(None);
        }
        let binder = Binder::open()?;
        let service = KeyMint::open(&binder)?;
        work(Some(&service))
    })();
    done.map_err(|e| format!("{e:#}"))
}
