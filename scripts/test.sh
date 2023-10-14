#!/bin/bash
set -e
pushd doveauth/src/doveauth
../../venv/bin/pytest
popd

online-tests/venv/bin/pytest online-tests/ -vrx --durations=5
