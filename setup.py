from setuptools import setup

with open("README.md", "r") as fh:
    long_description = fh.read()

setup(
    name="smartbox",
    version="0.0.5",
    author="Graham Bennett",
    author_email="graham@grahambennett.org",
    description="Python API to control heating 'smart boxes'",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/graham33/smartbox",
    packages=["smartbox"],
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
    ],
    python_requires=">=3.9",
    install_requires=[
        "aiohttp",
        "Click",
        "jq",
        # python-socketio major version needs to be synchronised with the
        # server socket.io version (socket.io has incompatible protocol
        # changes between major versions)
        "python-socketio>=4.6.0,<6.0.0",
        "requests",
        "websocket_client",
    ],
    tests_require=[
        "freezegun",
        "pytest",
        "pytest-asyncio",
        "pytest-mock",
        "pytest-randomly",
        "requests-mock",
    ],
    entry_points="""
      [console_scripts]
      smartbox=smartbox.cmd:smartbox
      """,
)
