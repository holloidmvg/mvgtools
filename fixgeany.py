#! /usr/bin/env python

import os
import sys
import platform
import configparser
from pathlib import Path

# Attempt screen resolution detection across OS platforms
def get_horizontal_resolution():
    try:
        if platform.system() == "Windows":
            import ctypes
            user32 = ctypes.windll.user32
            # Handles High-DPI scaling gracefully on Windows
            user32.SetProcessDPIAware()
            return user32.GetSystemMetrics(0)
        else:
            # Linux / Unix X11 / Wayland via xrandr or tkinter fallbacks
            import subprocess
            try:
                output = subprocess.check_output("xrandr | grep '*' | head -n 1", shell=True).decode()
                res_str = output.split()[0]
                return int(res_str.split('x')[0])
            except Exception:
                import tkinter as tk
                root = tk.Tk()
                root.withdraw()
                return root.winfo_screenwidth()
    except Exception:
        # Fallback default if display check fails
        return 1920

def get_paths():
    system = platform.system()
    if system == "Windows":
        appdata = Path(os.getenv("APPDATA", r"C:\Users\Public\AppData\Roaming"))
        geany_dir = appdata / "geany"
        geany_conf = geany_dir / "geany.conf"
        keybindings_conf = geany_dir / "keybindings.conf"
        # Windows GTK3 settings path
        gtk_settings = Path(r"C:\Program Files\Geany\share\gtk-3.0\settings.ini")
    else: # Linux / Unix
        config_home = Path(os.getenv("XDG_CONFIG_HOME", Path.home() / ".config"))
        geany_dir = config_home / "geany"
        geany_conf = geany_dir / "geany.conf"
        keybindings_conf = geany_dir / "keybindings.conf"
        gtk_settings = config_home / "gtk-3.0" / "settings.ini"
        
    return geany_conf, keybindings_conf, gtk_settings

def update_geany_conf(conf_path: Path):
    if not conf_path.exists():
        print(f"[!] geany.conf not found at {conf_path}. Skipping update.")
        return

    config = configparser.ConfigParser(strict=False)
    config.optionxform = str
    config.read(conf_path, encoding="utf-8")

    h_res = get_horizontal_resolution()
    if h_res < 2000:
      font_size = "9"
    elif h_res < 2800:
      font_size = "12"
    else:
      font_size = "18"
    target_font = f"Hack {font_size}"
    print(f"[*] Detected screen width: {h_res}px -> Setting font to '{target_font}'")

    target_settings = {
        "color_scheme": "solarized-dark.conf",
        "editor_font": target_font,
        "tagbar_font": target_font,
        "msgwin_font": target_font,
        "check_detect_indent": "false",
        "detect_indent_width": "false",
        "mru_length": "0",
        "long_line_column": "80",
        "auto_close_xml_tags": "false",
        "complete_snippets": "false",
        "auto_continue_multiline": "false",
        "pref_editor_tab_width": "2",
        "indent_mode": "0",
        "indent_type": "0",
    }

    if "geany" not in config:
        config["geany"] = {}

    keys_to_remove = [
        key for key in config["geany"].keys()
        if key.startswith("pref_main") or key.startswith("use_")
    ]

    for k in keys_to_remove:
        del config["geany"][k]

    for key, val in target_settings.items():
        config["geany"][key] = val

    with conf_path.open("w", encoding="utf-8") as f:
        config.write(f, space_around_delimiters=True)
    print(f"[+] Successfully updated {conf_path}")

def update_keybindings_conf(keybind_path: Path):
    # Ensure geany directory exists if writing keybindings from scratch
    keybind_path.parent.mkdir(parents=True, exist_ok=True)

    config = configparser.ConfigParser(strict=False)
    config.optionxform = str

    if keybind_path.exists():
        config.read(keybind_path, encoding="utf-8")

    # Target keybindings
    bindings = {
        "edit_increaseindent": "<Primary><Shift>parenright",
        "edit_decreaseindent": "<Primary><Shift>parenleft",
        "edit_joinlines" : "<Primary><Shift>j",
        "menu_redo" : "<Primary><Shift>z",
    }
    shiftcolumn_bindings = {
        "shift_left": "<Primary>9",
        "shift_right": "<Primary>0",
    }

    if "Bindings" not in config:
        config["Bindings"] = {}
    for key, val in bindings.items():
        config["Bindings"][key] = val

    if "shiftcolumn" not in config:
        config["shiftcolumn"] = {}
    for key, val in shiftcolumn_bindings.items():
        config["shiftcolumn"][key] = val

    with keybind_path.open("w", encoding="utf-8") as f:
        config.write(f)
    print(f"[+] Successfully updated {keybind_path}")

def update_gtk_dark_mode(settings_path: Path):
    settings_path.parent.mkdir(parents=True, exist_ok=True)

    config = configparser.ConfigParser(strict=False)
    config.optionxform = str

    if settings_path.exists():
        config.read(settings_path, encoding="utf-8")

    if "Settings" not in config:
        config["Settings"] = {}

    config["Settings"]["gtk-application-prefer-dark-theme"] = "true"

    try:
        with settings_path.open("w", encoding="utf-8") as f:
            config.write(f)
        print(f"[+] Successfully enabled dark theme in {settings_path}")
    except PermissionError:
        print(f"[!] Permission denied when writing to {settings_path}. (On Windows, run script as Administrator).")

if __name__ == "__main__":
    geany_path, keybindings_path, gtk_path = get_paths()
    update_geany_conf(geany_path)
    update_keybindings_conf(keybindings_path)
    update_gtk_dark_mode(gtk_path)
