# coding=utf8
# --------------------------------------------------------
# Scene Graph Generation by Iterative Message Passing
# Licensed under The MIT License [see LICENSE for details]
# Written by Danfei Xu
# --------------------------------------------------------

import argparse, json, string
from collections import Counter
import math

from math import floor
import h5py as h5
import numpy as np
import pprint

import xml.etree.ElementTree as ET

"""
A script for generating an hdf5 ROIDB from the VisualGenome dataset
"""

def preprocess_object_labels(data, alias_dict={}):
    for img in data:
        for obj in img['objects']:
            obj['ids'] = [obj['object_id']]
            names = []
            for name in obj['names']:
                label = sentence_preprocess(name)
                if label in alias_dict:
                    label = alias_dict[label]
                names.append(label)
            obj['names'] = names


def preprocess_predicates(data, alias_dict={}):
    for img in data:
        for relation in img['relationships']:
            predicate = sentence_preprocess(relation['predicate'])
            if predicate in alias_dict:
                predicate = alias_dict[predicate]
            relation['predicate'] = predicate


def extract_object_token(data, obj_list=[], verbose=True):
    """ Builds a set that contains the object names. Filters infrequent tokens. """
    token_counter = Counter()
    for img in data:
        for region in img['objects']:
            for name in region['names']:
                if not obj_list or name in obj_list:
                    token_counter.update([name])
    tokens = set()
    # pick top N tokens
    token_counter_return = {}
    for token, count in token_counter.most_common():
        tokens.add(token)
        token_counter_return[token] = count
    if verbose:
        print(('Keeping %d / %d objects'
                  % (len(tokens), len(token_counter))))
    return tokens, token_counter_return


def extract_predicate_token(data, pred_list=[], verbose=True):
    """ Builds a set that contains the relationship predicates. Filters infrequent tokens. """
    token_counter = Counter()
    total = 0
    for img in data:
        for relation in img['relationships']:
            predicate = relation['predicate']
            if not pred_list or predicate in pred_list:
                token_counter.update([predicate])
            total += 1
    tokens = set()
    token_counter_return = {}
    for token, count in token_counter.most_common():
        tokens.add(token)
        token_counter_return[token] = count
    if verbose:
        print(('Keeping %d / %d predicates with enough instances'
                  % (len(tokens), len(token_counter))))
    return tokens, token_counter_return


def merge_duplicate_boxes(data):
    def IoU(b1, b2):
        if b1[2] <= b2[0] or \
            b1[3] <= b2[1] or \
            b1[0] >= b2[2] or \
            b1[1] >= b2[3]:
            return 0

        b1b2 = np.vstack([b1,b2])
        minc = np.min(b1b2, 0)
        maxc = np.max(b1b2, 0)
        union_area = (maxc[2]-minc[0])*(maxc[3]-minc[1])
        int_area = (minc[2]-maxc[0])*(minc[3]-maxc[1])
        return float(int_area)/float(union_area)

    def to_x1y1x2y2(obj):
        x1 = obj['x']
        y1 = obj['y']
        x2 = obj['x'] + obj['w']
        y2 = obj['y'] + obj['h']
        return np.array([x1, y1, x2, y2], dtype=np.int32)

    def inside(b1, b2):
        return b1[0] >= b2[0] and b1[1] >= b2[1] \
            and b1[2] <= b2[2] and b1[3] <= b2[3]

    def overlap(obj1, obj2):
        b1 = to_x1y1x2y2(obj1)
        b2 = to_x1y1x2y2(obj2)
        iou = IoU(b1, b2)
        if all(b1 == b2) or iou > 0.9: # consider as the same box
            return 1
        elif (inside(b1, b2) or inside(b2, b1))\
            and obj1['names'][0] == obj2['names'][0]: # same object inside the other
            return 2
        elif iou > 0.6 and obj1['names'][0] == obj2['names'][0]: # multiple overlapping same object
            return 3
        else:
            return 0  # no overlap

    num_merged = {1:0, 2:0, 3:0}
    print('merging boxes..')
    for img in data:
        # mark objects to be merged and save their ids
        objs = img['objects']
        num_obj = len(objs)
        for i in range(num_obj):
            if 'M_TYPE' in objs[i]:  # has been merged
                continue
            merged_objs = [] # circular refs, but fine
            for j in range(i+1, num_obj):
                if 'M_TYPE' in objs[j]:  # has been merged
                    continue
                overlap_type = overlap(objs[i], objs[j])
                if overlap_type > 0:
                    objs[j]['M_TYPE'] = overlap_type
                    merged_objs.append(objs[j])
            objs[i]['mobjs'] = merged_objs

        # merge boxes
        filtered_objs = []
        merged_num_obj = 0
        for obj in objs:
            if 'M_TYPE' not in obj:
                ids = [obj['object_id']]
                dims = [to_x1y1x2y2(obj)]
                prominent_type = 1
                for mo in obj['mobjs']:
                    ids.append(mo['object_id'])
                    obj['names'].extend(mo['names'])
                    dims.append(to_x1y1x2y2(mo))
                    if mo['M_TYPE'] > prominent_type:
                        prominent_type = mo['M_TYPE']
                merged_num_obj += len(ids)
                obj['ids'] = ids
                mdims = np.zeros(4)
                if prominent_type > 1: # use extreme
                    mdims[:2] = np.min(np.vstack(dims)[:,:2], 0)
                    mdims[2:] = np.max(np.vstack(dims)[:,2:], 0)
                else:  # use mean
                    mdims = np.mean(np.vstack(dims), 0)
                obj['x'] = int(mdims[0])
                obj['y'] = int(mdims[1])
                obj['w'] = int(mdims[2] - mdims[0])
                obj['h'] = int(mdims[3] - mdims[1])

                num_merged[prominent_type] += len(obj['mobjs'])

                obj['mobjs'] = None
                obj['names'] = list(set(obj['names']))  # remove duplicates

                filtered_objs.append(obj)
            else:
                assert 'mobjs' not in obj

        img['objects'] = filtered_objs
        assert(merged_num_obj == num_obj)

    print('# merged boxes per merging type:')
    print(num_merged)


def build_token_dict(vocab):
    """ build bi-directional mapping between index and token"""
    token_to_idx, idx_to_token = {}, {}
    next_idx = 1
    vocab_sorted = sorted(list(vocab)) # make sure it's the same order everytime
    for token in vocab_sorted:
        token_to_idx[token] = next_idx
        idx_to_token[next_idx] = token
        next_idx = next_idx + 1

    return token_to_idx, idx_to_token


def encode_box(region, org_h, org_w, im_long_size):
    x = region['x']
    y = region['y']
    w = region['w']
    h = region['h']
    scale = float(im_long_size) / max(org_h, org_w)
    image_size = im_long_size
    # recall: x,y are 1-indexed
    x, y = math.floor(scale*(region['x']-1)), math.floor(scale*(region['y']-1))
    w, h = math.ceil(scale*region['w']), math.ceil(scale*region['h'])

    # clamp to image
    if x < 0: x = 0
    if y < 0: y = 0

    # box should be at least 2 by 2
    if x > image_size - 2:
        x = image_size - 2
    if y > image_size - 2:
        y = image_size - 2
    if x + w >= image_size:
        w = image_size - x
    if y + h >= image_size:
        h = image_size - y

    # also convert to center-coord oriented
    box = np.asarray([x+floor(w/2), y+floor(h/2), w, h], dtype=np.int32)
    assert box[2] > 0  # width height should be positive numbers
    assert box[3] > 0
    return box


def encode_objects(obj_data, token_to_idx, token_counter, org_h, org_w, im_long_sizes):
    encoded_labels = []
    encoded_boxes  = {}
    for size in im_long_sizes:
        encoded_boxes[size] = []
    im_to_first_obj = np.zeros(len(obj_data), dtype=np.int32)
    im_to_last_obj = np.zeros(len(obj_data), dtype=np.int32)
    obj_counter = 0

    for i, img in enumerate(obj_data):
        im_to_first_obj[i] = obj_counter
        img['id_to_idx'] = {}  # object id to region idx
        for obj in img['objects']:
           # pick a label for the object
            max_occur = 0
            obj_label = None
            for name in obj['names']:
                # pick the name that has maximum occurance
                if name in token_to_idx and token_counter[name] > max_occur:
                    obj_label = name
                    max_occur = token_counter[obj_label]

            if obj_label is not None:
                # encode region
                for size in im_long_sizes:
                    encoded_boxes[size].append(encode_box(obj, org_h[i], org_w[i], size))

                encoded_labels.append(token_to_idx[obj_label])

                for obj_id in obj['ids']: # assign same index for merged ids
                    img['id_to_idx'][obj_id] = obj_counter

                obj_counter += 1


        if im_to_first_obj[i] == obj_counter:
            im_to_first_obj[i] = -1
            im_to_last_obj[i] = -1
        else:
            im_to_last_obj[i] = obj_counter - 1

    for k, boxes in encoded_boxes.items():
        # print(boxes)
        encoded_boxes[k] = np.vstack(boxes)
    return np.vstack(encoded_labels), encoded_boxes, im_to_first_obj, im_to_last_obj


def encode_relationship(sub_id, obj_id, id_to_idx):
    # builds a tuple of the index of object and subject in the object list
    sub_idx = id_to_idx[sub_id]
    obj_idx = id_to_idx[obj_id]
    return np.asarray([sub_idx, obj_idx], dtype=np.int32)


def encode_relationships(rel_data, token_to_idx, obj_data):
    """MUST BE CALLED AFTER encode_objects!!!"""
    encoded_pred = []  # encoded predicates
    encoded_rel = []  # encoded relationship tuple
    im_to_first_rel = np.zeros(len(rel_data), dtype=np.int32)
    im_to_last_rel = np.zeros(len(rel_data), dtype=np.int32)
    rel_idx_counter = 0

    no_rel_counter = 0
    obj_filtered = 0
    predicate_filtered = 0
    duplicate_filtered = 0
    for i, img in enumerate(rel_data):
        im_to_first_rel[i] = rel_idx_counter
        id_to_idx = obj_data[i]['id_to_idx']  # object id to object list idx
        for relation in img['relationships']:
            subj = relation['subject']
            obj = relation['object']
            predicate = relation['predicate']
            if subj['object_id'] not in id_to_idx or obj['object_id'] not in id_to_idx:
                obj_filtered += 1
                continue
            elif predicate not in token_to_idx:
                predicate_filtered += 1
                continue
            elif id_to_idx[subj['object_id']] == id_to_idx[obj['object_id']]: # sub and obj can't be the same box
                duplicate_filtered += 1
                continue
            else:
                encoded_pred.append(token_to_idx[predicate])
                encoded_rel.append(
                    encode_relationship(subj['object_id'],
                                        obj['object_id'],
                                        id_to_idx
                                        ))
                rel_idx_counter += 1  # accumulate counter

        if im_to_first_rel[i] == rel_idx_counter:
            # if no qualifying relationship
            im_to_first_rel[i] = -1
            im_to_last_rel[i] = -1
            no_rel_counter += 1
        else:
            im_to_last_rel[i] = rel_idx_counter - 1
    print('%i rel is filtered by object' % obj_filtered)
    print('%i rel is filtered by predicate' % predicate_filtered)
    print('%i rel is filtered by duplicate' % duplicate_filtered)
    print('%i rel remains ' % len(encoded_pred))

    print('%i out of %i valid images have relationships' % (len(rel_data)-no_rel_counter, len(rel_data)))
    return np.vstack(encoded_pred), np.vstack(encoded_rel), im_to_first_rel, im_to_last_rel


def sentence_preprocess(phrase):
    """ preprocess a sentence: lowercase, clean up weird chars, remove punctuation """
    replacements = {
      '½': 'half',
      '—' : '-',
      '™': '',
      '¢': 'cent',
      'ç': 'c',
      'û': 'u',
      'é': 'e',
      '°': ' degree',
      'è': 'e',
      '…': '',
    }
    phrase = phrase.encode('utf-8')
    phrase = phrase.lstrip(' ').rstrip(' ')
    for k, v in replacements.iteritems():
        phrase = phrase.replace(k, v)
    return str(phrase).lower().translate(None, string.punctuation).decode('utf-8', 'ignore')


def encode_splits(obj_data, opt=None):
    if opt is not None:
        val_begin_idx = opt['val_begin_idx']
        test_begin_idx = opt['test_begin_idx']
    split = np.zeros(len(obj_data), dtype=np.int32)
    for i, info in enumerate(obj_data):
        splitix = 0
        if opt is None: # use encode from input file
            s = info['split']
            if s == 'val': splitix = 1
            if s == 'test': splitix = 2
        else: # use portion split
            if i >= val_begin_idx: splitix = 1
            if i >= test_begin_idx: splitix = 2
        split[i] = splitix
    if opt is not None and opt['shuffle']:
        np.random.shuffle(split)

    print(('assigned %d/%d/%d to train/val/test split' % (np.sum(split==0), np.sum(split==1), np.sum(split==2))))
    return split


def make_alias_dict(dict_file):
    """create an alias dictionary from a file"""
    out_dict = {}
    vocab = []
    for line in open(dict_file, 'r'):
        alias = line.strip('\n').strip('\r').split(',')
        alias_target = alias[0] if alias[0] not in out_dict else out_dict[alias[0]]
        for a in alias:
            out_dict[a] = alias_target  # use the first term as the aliasing target
        vocab.append(alias_target)
    return out_dict, vocab


def make_list(list_file):
    """create a blacklist list from a file"""
    return [line.strip('\n').strip('\r') for line in open(list_file)]


def filter_object_boxes(data, heights, widths, area_frac_thresh):
    """
    filter boxes by a box area-image area ratio threshold
    """
    thresh_count = 0
    all_count = 0
    for i, img in enumerate(data):
        filtered_obj = []
        area = float(heights[i]*widths[i])
        for obj in img['objects']:
            if float(obj['h'] * obj['w']) > area * area_frac_thresh:
                filtered_obj.append(obj)
                thresh_count += 1
            all_count += 1
        img['objects'] = filtered_obj
    print('box threshod: keeping %i/%i boxes' % (thresh_count, all_count))


def filter_by_idx(data, valid_list):
    return [data[i] for i in valid_list]


def obj_rel_cross_check(obj_data, rel_data, verbose=False):
    """
    make sure all objects that are in relationship dataset
    are in object dataset
    """
    num_img = len(obj_data)
    num_correct = 0
    total_rel = 0
    for i in xrange(num_img):
        assert(obj_data[i]['image_id'] == rel_data[i]['image_id'])
        objs = obj_data[i]['objects']
        rels = rel_data[i]['relationships']
        ids = [obj['object_id'] for obj in objs]
        for rel in rels:
            if rel['subject']['object_id'] in ids \
                and rel['object']['object_id'] in ids:
                num_correct += 1
            elif verbose:
                if rel['subject']['object_id'] not in ids:
                    print(str(rel['subject']['object_id']) + 'cannot be found in ' + str(i))
                if rel['object']['object_id'] not in ids:
                    print(str(rel['object']['object_id']) + 'cannot be found in ' + str(i))
            total_rel += 1
    print('cross check: %i/%i relationship are correct' % (num_correct, total_rel))


def sync_objects(obj_data, rel_data):
    num_img = len(obj_data)
    for i in xrange(num_img):
        assert(obj_data[i]['image_id'] == rel_data[i]['image_id'])
        objs = obj_data[i]['objects']
        rels = rel_data[i]['relationships']

        ids = [obj['object_id'] for obj in objs]
        for rel in rels:
            if rel['subject']['object_id'] not in ids:
                rel_obj = rel['subject']
                rel_obj['names'] = [rel_obj['name']]
                objs.append(rel_obj)
            if rel['object']['object_id'] not in ids:
                rel_obj = rel['object']
                rel_obj['names'] = [rel_obj['name']]
                objs.append(rel_obj)

        obj_data[i]['objects'] = objs


def create_from_xml(img_data, args):
    pred_list = set()
    obj_list = set()
    obj_data, rel_data = [], []

    for im in img_data:
        tree = ET.parse('{}/{}.xml'.format(args.vrrvg_dir, im['image_id']))
        root = tree.getroot()
        obj_data.append({'objects':[], 'image_id':im['image_id']})
        rel_data.append({'relationships':[], 'image_id':im['image_id']})
        for child in root:

            if child.tag == 'object':
                name = str(child[0].text)
                object_id = int(child[1].text)
                xmin = int(child[3][0].text)
                ymin = int(child[3][1].text)
                xmax = int(child[3][2].text)
                ymax = int(child[3][3].text)
                w = xmax - xmin
                h = ymax - ymin
                obj_data[-1]['objects'].append({'x': xmin, 'y': ymin, 'w': w, 'h': h, 'object_id': object_id, 'names': [name]})
                obj_list.add(name)

            if child.tag == 'relation':
                subject_id = int(child[0].text)
                object_id = int(child[1].text)
                predicate = str(child[2].text)
                rel_data[-1]['relationships'].append({'object': {'object_id': object_id}, 'subject': {'object_id': subject_id}, 'predicate': predicate})
                pred_list.add(predicate)
    
    return list(obj_list), list(pred_list), obj_data, rel_data


def main(args):
    print('start')
    pprint.pprint(args)

    # read in the annotation data
    print('loading json files..')
    img_data = json.load(open(args.metadata_input))

    print('read image db from %s' % args.imdb)
    imdb = h5.File(args.imdb, 'r')
    num_im, _, _, _ = imdb['images'].shape
    img_long_sizes = [512, 1024]
    valid_im_idx = imdb['valid_idx'][:] # valid image indices
    img_ids = imdb['image_ids'][:]

    print('len(valid_im_idx)', len(valid_im_idx))
    print('len(img_ids)', len(img_ids))

    img_data = filter_by_idx(img_data, valid_im_idx)

    with open("image_data_.json", 'w') as f:
        json.dump(img_data, f)

    print('len(img_data) before filtering', len(img_data))


    obj_list, pred_list, obj_data, rel_data = create_from_xml(img_data, args)
    print("number of object classes", len(obj_list))
    print("number of predicate classes", len(pred_list))

    print('len(obj_data) after filtering', len(obj_data))
    print('len(rel_data) after filtering', len(rel_data))
    print('len(img_data) after filtering', len(img_data))

    # sanity check
    for i in xrange(num_im):
        assert(obj_data[i]['image_id'] \
               == rel_data[i]['image_id'] \
               == img_data[i]['image_id'] \
               == img_ids[i]
               )

    # may only load a fraction of the data
    if args.load_frac < 1:
        num_im = int(num_im*args.load_frac)
        obj_data = obj_data[:num_im]
        rel_data = rel_data[:num_im]
    print('processing %i images' % num_im)

    # sync objects from rel to obj_data
    sync_objects(obj_data, rel_data)

    obj_rel_cross_check(obj_data, rel_data)

    heights, widths = imdb['original_heights'][:], imdb['original_widths'][:]
    if args.min_box_area_frac > 0:
        # filter out invalid small boxes
        print('threshold bounding box by %f area fraction' % args.min_box_area_frac)
        filter_object_boxes(obj_data, heights, widths, args.min_box_area_frac) # filter by box dimensions

    merge_duplicate_boxes(obj_data)

    # build vocabulary
    object_tokens, object_token_counter = extract_object_token(obj_data, obj_list)

    label_to_idx, idx_to_label = build_token_dict(object_tokens)

    predicate_tokens, predicate_token_counter = extract_predicate_token(rel_data, pred_list)
    predicate_to_idx, idx_to_predicate = build_token_dict(predicate_tokens)

    # print out vocabulary
    print('objects: ')
    print(object_token_counter)
    print('relationships: ')
    print(predicate_token_counter)

    # write the h5 file
    f = h5.File(args.h5_file, 'w')

    # encode object
    encoded_label, encoded_boxes, im_to_first_obj, im_to_last_obj = \
    encode_objects(obj_data, label_to_idx, object_token_counter, \
                   heights, widths, img_long_sizes)

    f.create_dataset('labels', data=encoded_label)
    for k, boxes in encoded_boxes.items():
        f.create_dataset('boxes_%i' % k, data=boxes)
    f.create_dataset('img_to_first_box', data=im_to_first_obj)
    f.create_dataset('img_to_last_box', data=im_to_last_obj)

    encoded_predicate, encoded_rel, im_to_first_rel, im_to_last_rel = \
    encode_relationships(rel_data, predicate_to_idx, obj_data)

    f.create_dataset('predicates', data=encoded_predicate)
    f.create_dataset('relationships', data=encoded_rel)
    f.create_dataset('img_to_first_rel', data=im_to_first_rel)
    f.create_dataset('img_to_last_rel', data=im_to_last_rel)

   # build train/val/test splits

    print('num objects = %i' % encoded_label.shape[0])
    print('num relationships = %i' % encoded_predicate.shape[0])

    opt = None
    if not args.use_input_split:
        opt = {}
        opt['val_begin_idx'] = int(len(obj_data) * args.train_frac)
        opt['test_begin_idx'] = int(len(obj_data) * args.val_frac)
        opt['shuffle'] = args.shuffle
    split = encode_splits(obj_data, opt)

    if split is not None:
        f.create_dataset('split', data=split) # 1 = test, 0 = train

    # and write the additional json file
    json_struct = {
        'label_to_idx': label_to_idx,
        'idx_to_label': idx_to_label,
        'predicate_to_idx': predicate_to_idx,
        'idx_to_predicate': idx_to_predicate,
        'predicate_count': predicate_token_counter,
        'object_count': object_token_counter
    }

    with open(args.json_file, 'w') as f:
        json.dump(json_struct, f)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--imdb', default='VG/imdb_1024.h5', type=str)
    parser.add_argument('--object_input', default='VG/objects.json', type=str)
    parser.add_argument('--relationship_input', default='VG/relationships.json', type=str)
    parser.add_argument('--vrrvg_dir', default='VG/VrR-VG', type=str)
    parser.add_argument('--metadata_input', default='VG/image_data.json', type=str)

    # parser.add_argument('--object_alias', default='VG/object_alias.txt', type=str)
    # parser.add_argument('--pred_alias', default='VG/predicate_alias.txt', type=str)
    # parser.add_argument('--object_list', default='VG/object_list.txt', type=str)
    # parser.add_argument('--pred_list', default='VG/predicate_list.txt', type=str)
    # parser.add_argument('--num_objects', default=150, type=int, help="set to 0 to disable filtering")
    # parser.add_argument('--num_predicates', default=50, type=int, help="set to 0 to disable filtering")
    parser.add_argument('--min_box_area_frac', default=0.002, type=float)
    parser.add_argument('--json_file', default='VG-dicts.json')
    parser.add_argument('--h5_file', default='VG.h5')
    parser.add_argument('--load_frac', default=1, type=float)
    parser.add_argument('--use_input_split', default=False, type=bool)
    parser.add_argument('--train_frac', default=0.7, type=float)
    parser.add_argument('--val_frac', default=0.7, type=float)
    parser.add_argument('--shuffle', default=False, type=bool)

    args = parser.parse_args()
    main(args)
