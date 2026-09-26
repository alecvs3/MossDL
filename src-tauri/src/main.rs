#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    #[cfg(all(windows, debug_assertions))]
    unsafe {
        use windows_sys::Win32::System::Console::{AllocConsole, AttachConsole, ATTACH_PARENT_PROCESS, SetConsoleTitleW};
        if AttachConsole(ATTACH_PARENT_PROCESS) == 0 {
            AllocConsole();
        }
        let title: Vec<u16> = "MossDL Dev Console [Debug Diagnostics]\0".encode_utf16().collect();
        SetConsoleTitleW(title.as_ptr());
        eprintln!("============================================================");
        eprintln!(" MossDL Dev Console initialized - Verbose diagnostics active");
        eprintln!("============================================================");
    }

    transfer_manager_lib::run()
}
