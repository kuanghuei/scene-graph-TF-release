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

python vg_to_roidb_vrr_fullVGv2_coco_aware.py \
    --imdb $IMDB \
    --json_file $OUT_PATH/$JSON \
    --h5_file $OUT_PATH/$H5 \
    --load_frac $FRAC \
    --min_box_area_frac 0.002 \
    # --num_objects $N_OBJ \
    # --num_predicates $N_REL \
