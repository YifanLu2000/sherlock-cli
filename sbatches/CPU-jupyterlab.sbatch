#!/bin/bash

PORT=$1
NOTEBOOK_DIR=${2:-${SCRATCH}}
cd $NOTEBOOK_DIR

module load python/3.12.1 py-pytorch/2.4.1_py312 opencv/4.10.0 x11/7.7 py-jupyterlab/4.3.2_py312 system
module load opencv/4.10.0

jupyter lab --no-browser --port=$PORT

