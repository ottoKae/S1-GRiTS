# setup.py
#
# S1-GRiTS is a pure-Python package: all project metadata lives in
# pyproject.toml and there are no compiled (.pyd/.so) extension modules to
# package. Builds therefore produce a portable `py3-none-any` wheel and no
# platform-specific wheel-tag machinery is required.
#
# This thin shim remains only so that legacy `python setup.py ...` invocations
# and editable installs keep working; setuptools reads everything it needs from
# pyproject.toml.

from setuptools import setup

setup()
