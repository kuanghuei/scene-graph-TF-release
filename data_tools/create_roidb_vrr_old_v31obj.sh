#!/bin/bash
set -x
set -e

OUT_PATH="."

# N_OBJ=150 # number of object categories
# N_REL=50 # number of relationship categories

H5=VG-SGG.h5
JSON=VG-SGG-dicts.json
FRAC=1
IMDB=imdb_1024.h5

python vg_to_roidb_vrr_old_v31obj.py \
    --imdb $IMDB \
    --json_file $OUT_PATH/$JSON \
    --h5_file $OUT_PATH/$H5 \
    --load_frac $FRAC \
    --train_frac 0.95 \
    --val_frac 0.95 \
    --min_box_area_frac 0.002 \
    --external_VG_SGG_dicts vrr_meta_v31 \
    # --num_objects $N_OBJ \
    # --num_predicates $N_REL \
