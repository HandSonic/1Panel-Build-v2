#!/usr/bin/env python3
"""Apply exact manifest/source-lock-bound repairs from the run contract."""
import sys
from pathlib import Path
from resolved_contract import runtime_contract
from lock_catalog import apply


def repair(root, version):
    contract = runtime_contract(version)
    apply(root, contract)
    if contract['frontend_lock']['kind'] == 'derived':
        print('Applied content-addressed repair: ' + contract['frontend_lock']['sha256'])


if __name__ == '__main__':
    repair(Path(sys.argv[1]), sys.argv[2])
