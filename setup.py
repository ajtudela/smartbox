from setuptools import setup

with open("README.md", "r") as fh:
    long_description = fh.read()

setup(
    name="smartbox",
    version="2.1.0-beta.1",
    author="Delmael",
    author_email="delmael@outlook.com",
    description="Python API to control heating 'smart boxes'",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/delmael/smartbox",
    packages=["smartbox"],
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
    ],
    python_requires=">=3.12",
    install_requires=[
        "aiohttp",
        "asyncclick",
        "jq",
        # python-socketio major version needs to be synchronised with the
        # server socket.io version (socket.io has incompatible protocol
        # changes between major versions)
        "python-socketio>=4.6.0,<5.0.0",
        "requests",
        "websocket_client",
        "pydantic==2.10.4",
        "jq",
    ],
    tests_require=[
        "freezegun",
        "pytest",
        "pytest-asyncio",
        "pytest-mock",
        "pytest-randomly",
        "requests-mock",
        "tox",
        "pytest-cov",
    ],
    entry_points="""
      [console_scripts]
      smartbox=smartbox.cmd:smartbox
      """,
)
