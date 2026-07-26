from setuptools import setup, find_packages

setup(
    name="control-center",
    version="1.2.0",
    description="Unified multi-OS control center for CUA actuation",
    author="Kartik A (NullVoider)",
    license="GPL-3.0",
    packages=find_packages(),
    install_requires=[
        "grpcio>=1.60.0",
        "grpcio-tools>=1.60.0",
        "protobuf>=4.25.0",
        "click>=8.1.0",
        "requests>=2.31.0",
        "cryptography>=42.0.0",
        "keyring>=24.0.0",
        # Imported unconditionally by integrations/status.py and integrations/export.py,
        # and integrations/__init__.py imports export — so without psutil the whole
        # integrations package, including the gRPC client, fails to import.
        "psutil>=5.9.0",
    ],
    entry_points={
        'console_scripts': [
            'control-center=controller.management.cli:main',
        ],
    },
    python_requires=">=3.8",
)