#!/usr/bin/env bash

if test ! -d ".venv"
then
    python3 -m venv .venv
fi

shasum=`which shasum`
checksum_file=".venv/.checksum"
venvchecksum=$(cat "${checksum_file}" 2>&1)

if test -z "${shasum}"
then
    checksum=
else
    checksum=$(cat requirements.txt constraints.txt |${shasum} -a 256 |awk ' { print $1 } ')
fi

source .venv/bin/activate

if test "${checksum}" != "${venvchecksum}"
then
    echo "Updating Python virtual environment. If this takes longer than a few minutes, it's probably stalled."
    pip3 install -q -U pip wheel
    pip3 install -q -U -r requirements.txt
    if test $? -eq 0
    then
        echo "${checksum}" >"${checksum_file}"
    else
        exit 1
    fi
fi

export FLASK_APP="application.factory:create_app('cli')"

if test "$(basename $0)" = "zappa.sh"
then
    zappa "$@"
else
    flask "$@"
fi
