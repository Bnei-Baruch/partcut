#!/bin/bash
# HQ lesson parts cut from converted full lessons. See /opt/filer/parts/partcut/cli.py
#   ./parts.sh cut --dates 2010-01-01            step 1: staging + report, no MDB writes
#   ./parts.sh register --report parts/reports/<run>.json [--uids a,b] [--dry-run]   step 2
# numpy's OpenBLAS: with its own thread pool every small vector op is ~2000x slower here and calls from
# partcut's worker threads can hang (seen 2026-11-25). partcut parallelises itself -> BLAS single-threaded.
export OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1
cd /opt/filer/parts && exec nice -n 19 ./venv/bin/python -m partcut.cli "$@"
