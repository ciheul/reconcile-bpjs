#!/bin/bash

#ROOT=/home/ciheul
ROOT=/home/ciheul/Projects

cd

source /home/ciheul/Projects/virtualenv/axes/bin/activate

cd $ROOT/reconcile-bpjs

echo "run"

$ROOT/virtualenv/axes/bin/python $ROOT/reconcile-bpjs/run.py
