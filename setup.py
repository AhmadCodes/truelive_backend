from setuptools import setup
from cx_Freeze import setup, Executable
import sys

build_exe_options = {
    "packages": [
        "streamlit",
        "streamlit.web.bootstrap",
        "streamlit.web.server",
        "streamlit.web.server.server",
        "tornado",
        "altair",
        "pandas",
        "numpy"
    ],
    "excludes": [],
    "include_files": [
        "main.py",
        "site_config.json",
        "camera_config.json"
    ],
    "include_msvcr": True,
}

base = None
if sys.platform == "win32":
    base = "Win32GUI"

setup(
    name="StreamConfiguration",
    version="1.0",
    description="Your Streamlit Application",
    options={
        "build_exe": build_exe_options,
    },
    executables=[
        Executable(
            "server.py",
            base=base,
            target_name="Stream Configuration.exe"
        )
    ]
)