"""Setup script for sqlite-note-store Hermes Agent plugin."""

from setuptools import setup, find_packages

setup(
    name="sqlite-note-store",
    version="0.1.0",
    description="SQLite-backed note repository as Hermes Agent external long-term memory "
                "— tool-compatible with markdown-note-store, entry-level writes, "
                "on-demand Markdown export.",
    long_description=open("README.md", encoding="utf-8").read(),
    long_description_content_type="text/markdown",
    author="Hermes Agent Community",
    packages=find_packages(),
    include_package_data=True,
    package_data={
        "sqlite_note_store": [
            "plugin.yaml",
            "skills/note-maintenance/SKILL.md",
            "dashboard/manifest.json",
            "dashboard/plugin_api.py",
            "dashboard/smoke_test.py",
            "dashboard/dist/index.js",
            "dashboard/dist/style.css",
        ],
    },
    python_requires=">=3.10",
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: MIT License",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
    ],
)
