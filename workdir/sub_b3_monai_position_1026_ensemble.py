#!/usr/bin/env python3

import argparse
from collections import defaultdict
from pathlib import Path
import pickle
import sys
import time

import numpy as np
import pandas as pd
import torch
import torch.optim
from torch.utils.data import DataLoader
from tqdm.auto import tqdm

import src.configuration as C
from src.models import get_img_model, ImgModelPE, ImgModel
import src.utils as utils
from src.utils import get_logger
from src.criterion import ImgLoss
from src.datasets import RsnaDatasetTest, RsnaDatasetTest2, RsnaDatasetTest3, RsnaDatasetTest3Valid
from src.postprocess import calib_p

from src import monaimodel2 as monaimodel
# from train import run_nn

# fix issue: "received 0 items of ancdata"
# https://github.com/pytorch/pytorch/issues/973#issuecomment-346405667
torch.multiprocessing.set_sharing_strategy('file_system')


def get_args():
    parser = argparse.ArgumentParser()
    # parser.add_argument("weight_path", help="weight path")
    parser.add_argument("--mode", type=str, default='both', help='for skip prediction private or public ids')   ### ADDED
    parser.add_argument("--debug", action='store_true', help="debug")
    parser.add_argument("--skip", action='store_true', help="skip for commit-time infer")
    parser.add_argument("--validation", action='store_true', help="for validation")
    return parser.parse_args()
args = get_args()

SEED = 42
DEVICE = "cuda"
def log(msg): print(msg)
def log_w(msg): print(msg)


import json
setting_json = open('SETTINGS.json', 'r')
setting_json = json.load(setting_json)

DATADIR = Path(setting_json["INPUT"])

_MEANS = {
    'pe_present_on_image': 0.053915069524414806,
    'negative_exam_for_pe': 0.6763928618101033,
    'rv_lv_ratio_gte_1': 0.12875001256566257,
    'rv_lv_ratio_lt_1': 0.17437230326919448,
    'leftsided_pe': 0.21089872969528548,
    'chronic_pe': 0.040139752506710064,
    'rightsided_pe': 0.2575653665766779,
    'acute_and_chronic_pe': 0.019458347341720122,
    'central_pe': 0.054468517151291695,
    'indeterminate': 0.020484822355039723}
_MEANS_POS = {
    'pe_present_on_image': 0.17786572188168448,
    'negative_exam_for_pe': 0.0,
    'rv_lv_ratio_gte_1': 0.4247460706119915,
    'rv_lv_ratio_lt_1': 0.5752539293880086,
    'leftsided_pe': 0.6957545475146886,
    'chronic_pe': 0.13242097466878175,
    'rightsided_pe': 0.849707702540123,
    'acute_and_chronic_pe': 0.0641930545038497,
    'central_pe': 0.1796915446534345,
    'indeterminate': 0.0}
_MEANS_NOT_POS = {
    'pe_present_on_image': 0.0,
    'negative_exam_for_pe': 0.9706048524432512,
    'rv_lv_ratio_gte_1': 0.0,
    'rv_lv_ratio_lt_1': 0.0,
    'leftsided_pe': 0.0,
    'chronic_pe': 0.0,
    'rightsided_pe': 0.0,
    'acute_and_chronic_pe': 0.0,
    'central_pe': 0.0,
    'indeterminate': 0.029395147556748744}

def load_sub_filled_average():
    sample_sub_path = "sample_submission.csv" if not args.validation else "validation/fold0.sample_submission.csv"
    sub = pd.read_csv(DATADIR / sample_sub_path)
    sub['label'] = _MEANS['pe_present_on_image']
    for feat in _MEANS.keys():
        sub.loc[sub.id.str.contains(feat, regex=False), 'label'] = _MEANS[feat]
    return sub


DO_PE_POS_IEFER = False
print("=== DO_PE_POS_IEFER", DO_PE_POS_IEFER)

def sigmoid(x):
    return 1 / (1 + np.exp(-x))

def inv_sigmoid(prob):
    return np.log(prob/(1-prob))

def one_study_user_stacking2(one_study_test, lgb_models, features):
    """df columns, SOPInstanceUID, z_pos, prediction_column1, ..."""
    one_study_test = one_study_test.sort_values('z_pos')
    # for pred_n in range(len(preds_list)):
    for pred_n in range(1):
        for i in range(1, 10):
            one_study_test[f'pred{pred_n}_pre{i}'] = one_study_test[f'pred{pred_n}'].shift(i)
            one_study_test[f'pred{pred_n}_post{i}'] = one_study_test[f'pred{pred_n}'].shift(-i)

    z_pos_max = one_study_test.z_pos.max()
    z_pos_min = one_study_test.z_pos.min()
    one_study_test['z_pos_norm'] = (one_study_test['z_pos'] - z_pos_min) / (z_pos_max - z_pos_min)

    test_preds = np.zeros(len( one_study_test.z_pos ))

    if args.debug:
        print("stacking feature:", features)
        print("stacking input\n", one_study_test[features])

    for model in lgb_models:
        test_preds += model.predict(one_study_test[features]) / len(lgb_models)
    one_study_test['stacking_pred'] = sigmoid(test_preds)
    return one_study_test.sort_index()


def grouping(df):  # for pos_exam
    grouped = pd.DataFrame(df.groupby('StudyInstanceUID')['pred'].mean())
    grouped = grouped.rename(columns={'pred': 'mean'})
    count = df.groupby('StudyInstanceUID')['pred'].count()
    grouped['count_total'] = count

    for i in range(1,10):
        count = df[df.pred>i/10].groupby('StudyInstanceUID')['pred'].count()
        grouped[f'count_over{i/10}'] = count
        grouped[f'count_over{i/10}_ratio'] = count / grouped['count_total']

    for q in [30, 50, 70, 80, 90, 95, 99]:
        grouped[f'percentile{q}'] = df.groupby('StudyInstanceUID')['pred'].apply(lambda arr: np.percentile(arr, q))

    ma = pd.DataFrame(df.groupby('StudyInstanceUID')['pred'].max())
    grouped['max'] = ma.pred

    grouped = grouped.reset_index().fillna(0)
    return grouped

def grouping_one(df, pred_col):
    grouped = pd.DataFrame(df.groupby('StudyInstanceUID')[pred_col].mean())
    grouped = grouped.rename(columns={pred_col: f'{pred_col}_mean'})
    count = df.groupby('StudyInstanceUID')[pred_col].count()
    grouped['count_total'] = count

    for i in range(1,10):
        count = df[df[pred_col]>i/10].groupby('StudyInstanceUID')[pred_col].count()
        grouped[f'{pred_col}_count_over{i/10}'] = count
        grouped[f'{pred_col}_count_over{i/10}_ratio'] = count / grouped['count_total']
    count = df[df[pred_col]==1].groupby('StudyInstanceUID')[pred_col].count()

    for q in [30, 50, 70, 80, 90, 95, 99]:
        grouped[f'{pred_col}_percentile{q}'] = df.groupby('StudyInstanceUID')[pred_col].apply(lambda arr: np.percentile(arr, q))

    ma = pd.DataFrame(df.groupby('StudyInstanceUID')[pred_col].max())
    grouped[f'{pred_col}_max'] = ma[pred_col]

    grouped = grouped.reset_index().fillna(0)
    del grouped['count_total']
    return grouped


def one_study_user_stacking_posexam2(one_study_test, lgb_models, features):
    one_study_test['StudyInstanceUID'] = '_dummy'
    test_grouped = grouping(one_study_test)
    

    if args.debug:
        print("posexam stacking feature:", features)
        print("posexam stacking input\n", test_grouped[features])

    test_preds = np.zeros(1)
    for model in lgb_models:
        # posexam model, predict() returns probability
        test_preds += inv_sigmoid( model.predict(test_grouped[features]) ) / len(lgb_models)
    
    return sigmoid(test_preds)[0]

def one_study_user_stacking_posexam3(test_grouped, lgb_models, features):
    # one_study_test['StudyInstanceUID'] = '_dummy'
    # test_grouped = grouping(one_study_test)
    

    if args.debug:
        print("posexam stacking feature:", features)
        print("posexam stacking input\n", test_grouped[features])

    test_preds = np.zeros(1)
    for model in lgb_models:
        # posexam model, predict() returns probability
        test_preds += inv_sigmoid( model.predict(test_grouped[features]) ) / len(lgb_models)
    
    return sigmoid(test_preds)[0]

def get_model_eval(model, weight_path):
    utils.load_model(weight_path, model)
    return model.cuda().eval()

def main():
    utils.set_seed(SEED)
    device = torch.device(DEVICE)

    # prepare Dataframe first
    TEST_PATH = "test.csv" if not args.validation else "validation/fold0.test.csv"
    df_test = pd.read_csv(DATADIR / TEST_PATH)
    df_sub = load_sub_filled_average()
    df_sub = df_sub.set_index('id')

    # sanity check
    for _std in df_test.StudyInstanceUID.unique():
        assert (len(df_sub.loc[_std + '_negative_exam_for_pe']) == 1)
    for _sop in df_test.SOPInstanceUID.unique():
        assert (len(df_sub.loc[_sop]) == 1)
    print("sanity check passed. df_test, df_sub prepared")

    print(len(df_test))
    if args.skip and len(df_test) == 146853:
        print("=== Only public test is vissible. Skip inference and just submit mean-prediction!")
        df_sub.reset_index(inplace=True)
        df_sub.to_csv("submission.csv", index=False)
        return


    # if args.mode == 'private':
    #     public_exam_ids_df = pd.read_pickle('../input/publicexam/StudyInstanceUID.pkl')
    #     df_test = df_test[~df_test.StudyInstanceUID.isin(public_exam_ids_df.StudyInstanceUID.values)]
    # elif args.mode == 'public':
    #     public_exam_ids_df = pd.read_pickle('../input/publicexam/StudyInstanceUID.pkl')
    #     df_test = df_test[df_test.StudyInstanceUID.isin(public_exam_ids_df.StudyInstanceUID.values)]


    # print( "=> len(df_test)", len(df_test))


    # MONAI, must to start from 0 and increment by 1 [fold0,fold1, ...]
    monai_models = [
        monaimodel.MonaiModelTest("output_jan/fold0.pth"),  # fold0
        monaimodel.MonaiModelTest("output_jan/fold1.pth"),  # fold1
        monaimodel.MonaiModelTest("output_jan/fold2.pth"),  # fold2
    ]

    # definie img-level models here
    img_models = {
        "fold0:b3_non_weight": [get_model_eval(ImgModel(archi="tf_efficientnet_b3_ns", pretrained=False), "output/final_image_level/fold0_ep0.pt"), 3.8250639579850194],
        "fold1:b3_non_weight": [get_model_eval(ImgModel(archi="tf_efficientnet_b3_ns", pretrained=False), "output/final_image_level/fold1_ep0.pt"), 8.555037588568537],
        "fold2:b3_non_weight": [get_model_eval(ImgModel(archi="tf_efficientnet_b3_ns", pretrained=False), "output/final_image_level/fold2_ep0.pt"), 4.374239635034443],
        "fold0:position": [get_model_eval(ImgModelPE(archi="tf_efficientnet_b0", pretrained=False), "output/final_position/fold0_ep0.pt"), 1],
        "fold1:position": [get_model_eval(ImgModelPE(archi="tf_efficientnet_b0", pretrained=False), "output/final_position/fold1_ep0.pt"), 1],
        "fold2:position": [get_model_eval(ImgModelPE(archi="tf_efficientnet_b0", pretrained=False), "output/final_position/fold2_ep0.pt"), 1],
    }

    # lgb_models = [pickle.load(open(f'lgb_models/lgb_fold{i}.pkl', 'rb')) for i in range(5)]
    lgb_models = [pickle.load(open(f'lgb_models/exp035_1018/lgb_seed0_fold{i}.pkl', 'rb')) for i in range(5)]
    features = ['pred0', 'pred0_post1', 'pred0_post2', 'pred0_post3', 'pred0_post4', 'pred0_post5', 'pred0_post6', 'pred0_post7', 'pred0_post8', 'pred0_post9', 'pred0_pre1', 'pred0_pre2', 'pred0_pre3', 'pred0_pre4', 'pred0_pre5', 'pred0_pre6', 'pred0_pre7', 'pred0_pre8', 'pred0_pre9', 'z_pos_norm']

    # lgb_models_posexam = [pickle.load(open(f'lgb_models/b3_exams_monai_position_1026/positive_exam_for_pe_monai_lgb_seed0_fold{i}.pkl', 'rb')) for i in range(5)]
    # features_exams_monai = ['count_over0.1', 'count_over0.1_ratio', 'count_over0.2', 'count_over0.2_ratio', 'count_over0.3', 'count_over0.3_ratio', 'count_over0.4', 'count_over0.4_ratio', 'count_over0.5', 'count_over0.5_ratio', 'count_over0.6', 'count_over0.6_ratio', 'count_over0.7', 'count_over0.7_ratio', 'count_over0.8', 'count_over0.8_ratio', 'count_over0.9', 'count_over0.9_ratio', 'max', 'mean', 'monai_acute_and_chronic_pe', 'monai_central_pe', 'monai_chronic_pe', 'monai_leftsided_pe', 'monai_rightsided_pe', 'monai_rv_lv_ratio_gte_1', 'percentile30', 'percentile50', 'percentile70', 'percentile80', 'percentile90', 'percentile95', 'percentile99', 'position_model_central_pe_count_over0.1', 'position_model_central_pe_count_over0.1_ratio', 'position_model_central_pe_count_over0.2', 'position_model_central_pe_count_over0.2_ratio', 'position_model_central_pe_count_over0.3', 'position_model_central_pe_count_over0.3_ratio', 'position_model_central_pe_count_over0.4', 'position_model_central_pe_count_over0.4_ratio', 'position_model_central_pe_count_over0.5', 'position_model_central_pe_count_over0.5_ratio', 'position_model_central_pe_count_over0.6', 'position_model_central_pe_count_over0.6_ratio', 'position_model_central_pe_count_over0.7', 'position_model_central_pe_count_over0.7_ratio', 'position_model_central_pe_count_over0.8', 'position_model_central_pe_count_over0.8_ratio', 'position_model_central_pe_count_over0.9', 'position_model_central_pe_count_over0.9_ratio', 'position_model_central_pe_max', 'position_model_central_pe_mean', 'position_model_central_pe_percentile30', 'position_model_central_pe_percentile50', 'position_model_central_pe_percentile70', 'position_model_central_pe_percentile80', 'position_model_central_pe_percentile90', 'position_model_central_pe_percentile95', 'position_model_central_pe_percentile99', 'position_model_leftsided_pe_count_over0.1', 'position_model_leftsided_pe_count_over0.1_ratio', 'position_model_leftsided_pe_count_over0.2', 'position_model_leftsided_pe_count_over0.2_ratio', 'position_model_leftsided_pe_count_over0.3', 'position_model_leftsided_pe_count_over0.3_ratio', 'position_model_leftsided_pe_count_over0.4', 'position_model_leftsided_pe_count_over0.4_ratio', 'position_model_leftsided_pe_count_over0.5', 'position_model_leftsided_pe_count_over0.5_ratio', 'position_model_leftsided_pe_count_over0.6', 'position_model_leftsided_pe_count_over0.6_ratio', 'position_model_leftsided_pe_count_over0.7', 'position_model_leftsided_pe_count_over0.7_ratio', 'position_model_leftsided_pe_count_over0.8', 'position_model_leftsided_pe_count_over0.8_ratio', 'position_model_leftsided_pe_count_over0.9', 'position_model_leftsided_pe_count_over0.9_ratio', 'position_model_leftsided_pe_max', 'position_model_leftsided_pe_mean', 'position_model_leftsided_pe_percentile30', 'position_model_leftsided_pe_percentile50', 'position_model_leftsided_pe_percentile70', 'position_model_leftsided_pe_percentile80', 'position_model_leftsided_pe_percentile90', 'position_model_leftsided_pe_percentile95', 'position_model_leftsided_pe_percentile99', 'position_model_pe_present_on_image_count_over0.1', 'position_model_pe_present_on_image_count_over0.1_ratio', 'position_model_pe_present_on_image_count_over0.2', 'position_model_pe_present_on_image_count_over0.2_ratio', 'position_model_pe_present_on_image_count_over0.3', 'position_model_pe_present_on_image_count_over0.3_ratio', 'position_model_pe_present_on_image_count_over0.4', 'position_model_pe_present_on_image_count_over0.4_ratio', 'position_model_pe_present_on_image_count_over0.5', 'position_model_pe_present_on_image_count_over0.5_ratio', 'position_model_pe_present_on_image_count_over0.6', 'position_model_pe_present_on_image_count_over0.6_ratio', 'position_model_pe_present_on_image_count_over0.7', 'position_model_pe_present_on_image_count_over0.7_ratio', 'position_model_pe_present_on_image_count_over0.8', 'position_model_pe_present_on_image_count_over0.8_ratio', 'position_model_pe_present_on_image_count_over0.9', 'position_model_pe_present_on_image_count_over0.9_ratio', 'position_model_pe_present_on_image_max', 'position_model_pe_present_on_image_mean', 'position_model_pe_present_on_image_percentile30', 'position_model_pe_present_on_image_percentile50', 'position_model_pe_present_on_image_percentile70', 'position_model_pe_present_on_image_percentile80', 'position_model_pe_present_on_image_percentile90', 'position_model_pe_present_on_image_percentile95', 'position_model_pe_present_on_image_percentile99', 'position_model_rightsided_pe_count_over0.1', 'position_model_rightsided_pe_count_over0.1_ratio', 'position_model_rightsided_pe_count_over0.2', 'position_model_rightsided_pe_count_over0.2_ratio', 'position_model_rightsided_pe_count_over0.3', 'position_model_rightsided_pe_count_over0.3_ratio', 'position_model_rightsided_pe_count_over0.4', 'position_model_rightsided_pe_count_over0.4_ratio', 'position_model_rightsided_pe_count_over0.5', 'position_model_rightsided_pe_count_over0.5_ratio', 'position_model_rightsided_pe_count_over0.6', 'position_model_rightsided_pe_count_over0.6_ratio', 'position_model_rightsided_pe_count_over0.7', 'position_model_rightsided_pe_count_over0.7_ratio', 'position_model_rightsided_pe_count_over0.8', 'position_model_rightsided_pe_count_over0.8_ratio', 'position_model_rightsided_pe_count_over0.9', 'position_model_rightsided_pe_count_over0.9_ratio', 'position_model_rightsided_pe_max', 'position_model_rightsided_pe_mean', 'position_model_rightsided_pe_percentile30', 'position_model_rightsided_pe_percentile50', 'position_model_rightsided_pe_percentile70', 'position_model_rightsided_pe_percentile80', 'position_model_rightsided_pe_percentile90', 'position_model_rightsided_pe_percentile95', 'position_model_rightsided_pe_percentile99']
    # ### features_posexam = ['count_over0.1', 'count_over0.1_ratio', 'count_over0.2', 'count_over0.2_ratio', 'count_over0.3', 'count_over0.3_ratio', 'count_over0.4', 'count_over0.4_ratio', 'count_over0.5', 'count_over0.5_ratio', 'count_over0.6', 'count_over0.6_ratio', 'count_over0.7', 'count_over0.7_ratio', 'count_over0.8', 'count_over0.8_ratio', 'count_over0.9', 'count_over0.9_ratio', 'max', 'mean', 'percentile30', 'percentile50', 'percentile70', 'percentile80', 'percentile90', 'percentile95', 'percentile99']

    lgb_models_each_target = {}
    # for _target in [ 'rv_lv_ratio_gte_1', "central_pe", "leftsided_pe", "rightsided_pe", "acute_and_chronic_pe", "chronic_pe", "rv_lv_ratio_lt_1"]:
    for _target in [ 'rv_lv_ratio_gte_1', "central_pe", "leftsided_pe", "rightsided_pe", "chronic_pe", "rv_lv_ratio_lt_1"] + ['positive_exam_for_pe']:
        lgb_models_each_target[_target] = [pickle.load(open(f'lgb_models/b3_exams_monai_position_1026/{_target}_monai_lgb_seed0_fold{i}.pkl', 'rb')) for i in range(5)]
    features_exams_monai = ['count_over0.1', 'count_over0.1_ratio', 'count_over0.2', 'count_over0.2_ratio', 'count_over0.3', 'count_over0.3_ratio', 'count_over0.4', 'count_over0.4_ratio', 'count_over0.5', 'count_over0.5_ratio', 'count_over0.6', 'count_over0.6_ratio', 'count_over0.7', 'count_over0.7_ratio', 'count_over0.8', 'count_over0.8_ratio', 'count_over0.9', 'count_over0.9_ratio', 'max', 'mean', 'monai_acute_and_chronic_pe', 'monai_central_pe', 'monai_chronic_pe', 'monai_leftsided_pe', 'monai_rightsided_pe', 'monai_rv_lv_ratio_gte_1', 'percentile30', 'percentile50', 'percentile70', 'percentile80', 'percentile90', 'percentile95', 'percentile99', 'position_model_central_pe_count_over0.1', 'position_model_central_pe_count_over0.1_ratio', 'position_model_central_pe_count_over0.2', 'position_model_central_pe_count_over0.2_ratio', 'position_model_central_pe_count_over0.3', 'position_model_central_pe_count_over0.3_ratio', 'position_model_central_pe_count_over0.4', 'position_model_central_pe_count_over0.4_ratio', 'position_model_central_pe_count_over0.5', 'position_model_central_pe_count_over0.5_ratio', 'position_model_central_pe_count_over0.6', 'position_model_central_pe_count_over0.6_ratio', 'position_model_central_pe_count_over0.7', 'position_model_central_pe_count_over0.7_ratio', 'position_model_central_pe_count_over0.8', 'position_model_central_pe_count_over0.8_ratio', 'position_model_central_pe_count_over0.9', 'position_model_central_pe_count_over0.9_ratio', 'position_model_central_pe_max', 'position_model_central_pe_mean', 'position_model_central_pe_percentile30', 'position_model_central_pe_percentile50', 'position_model_central_pe_percentile70', 'position_model_central_pe_percentile80', 'position_model_central_pe_percentile90', 'position_model_central_pe_percentile95', 'position_model_central_pe_percentile99', 'position_model_leftsided_pe_count_over0.1', 'position_model_leftsided_pe_count_over0.1_ratio', 'position_model_leftsided_pe_count_over0.2', 'position_model_leftsided_pe_count_over0.2_ratio', 'position_model_leftsided_pe_count_over0.3', 'position_model_leftsided_pe_count_over0.3_ratio', 'position_model_leftsided_pe_count_over0.4', 'position_model_leftsided_pe_count_over0.4_ratio', 'position_model_leftsided_pe_count_over0.5', 'position_model_leftsided_pe_count_over0.5_ratio', 'position_model_leftsided_pe_count_over0.6', 'position_model_leftsided_pe_count_over0.6_ratio', 'position_model_leftsided_pe_count_over0.7', 'position_model_leftsided_pe_count_over0.7_ratio', 'position_model_leftsided_pe_count_over0.8', 'position_model_leftsided_pe_count_over0.8_ratio', 'position_model_leftsided_pe_count_over0.9', 'position_model_leftsided_pe_count_over0.9_ratio', 'position_model_leftsided_pe_max', 'position_model_leftsided_pe_mean', 'position_model_leftsided_pe_percentile30', 'position_model_leftsided_pe_percentile50', 'position_model_leftsided_pe_percentile70', 'position_model_leftsided_pe_percentile80', 'position_model_leftsided_pe_percentile90', 'position_model_leftsided_pe_percentile95', 'position_model_leftsided_pe_percentile99', 'position_model_pe_present_on_image_count_over0.1', 'position_model_pe_present_on_image_count_over0.1_ratio', 'position_model_pe_present_on_image_count_over0.2', 'position_model_pe_present_on_image_count_over0.2_ratio', 'position_model_pe_present_on_image_count_over0.3', 'position_model_pe_present_on_image_count_over0.3_ratio', 'position_model_pe_present_on_image_count_over0.4', 'position_model_pe_present_on_image_count_over0.4_ratio', 'position_model_pe_present_on_image_count_over0.5', 'position_model_pe_present_on_image_count_over0.5_ratio', 'position_model_pe_present_on_image_count_over0.6', 'position_model_pe_present_on_image_count_over0.6_ratio', 'position_model_pe_present_on_image_count_over0.7', 'position_model_pe_present_on_image_count_over0.7_ratio', 'position_model_pe_present_on_image_count_over0.8', 'position_model_pe_present_on_image_count_over0.8_ratio', 'position_model_pe_present_on_image_count_over0.9', 'position_model_pe_present_on_image_count_over0.9_ratio', 'position_model_pe_present_on_image_max', 'position_model_pe_present_on_image_mean', 'position_model_pe_present_on_image_percentile30', 'position_model_pe_present_on_image_percentile50', 'position_model_pe_present_on_image_percentile70', 'position_model_pe_present_on_image_percentile80', 'position_model_pe_present_on_image_percentile90', 'position_model_pe_present_on_image_percentile95', 'position_model_pe_present_on_image_percentile99', 'position_model_rightsided_pe_count_over0.1', 'position_model_rightsided_pe_count_over0.1_ratio', 'position_model_rightsided_pe_count_over0.2', 'position_model_rightsided_pe_count_over0.2_ratio', 'position_model_rightsided_pe_count_over0.3', 'position_model_rightsided_pe_count_over0.3_ratio', 'position_model_rightsided_pe_count_over0.4', 'position_model_rightsided_pe_count_over0.4_ratio', 'position_model_rightsided_pe_count_over0.5', 'position_model_rightsided_pe_count_over0.5_ratio', 'position_model_rightsided_pe_count_over0.6', 'position_model_rightsided_pe_count_over0.6_ratio', 'position_model_rightsided_pe_count_over0.7', 'position_model_rightsided_pe_count_over0.7_ratio', 'position_model_rightsided_pe_count_over0.8', 'position_model_rightsided_pe_count_over0.8_ratio', 'position_model_rightsided_pe_count_over0.9', 'position_model_rightsided_pe_count_over0.9_ratio', 'position_model_rightsided_pe_max', 'position_model_rightsided_pe_mean', 'position_model_rightsided_pe_percentile30', 'position_model_rightsided_pe_percentile50', 'position_model_rightsided_pe_percentile70', 'position_model_rightsided_pe_percentile80', 'position_model_rightsided_pe_percentile90', 'position_model_rightsided_pe_percentile95', 'position_model_rightsided_pe_percentile99']
    print("len(features_exams_monai)", len(features_exams_monai))

    ### result_df_dict, monai_dict = sub_5(None, img_models, monai_models)
    result_df_dict, monai_dict = sub_5(None, img_models, monai_models, df_test)
    # monai_df = monai_df.set_index("StudyInstanceUID")

    for study in df_test.StudyInstanceUID.unique():

        result_df = result_df_dict[study]
        # monai_result = monai_df.loc[study]
        monai_result_arr = monai_dict[study]
        result_df["StudyInstanceUID"] = study

        ### df_sub.loc[stacking_pred_df['SOPInstanceUID'], 'label'] = stacking_pred_df['stacking_pred'].values
        FOLDS = [0, 1, 2]
        # FOLDS = [1]
        _res = np.zeros(len(result_df.z_pos))
        
        for fold in FOLDS:
            _df = stacking_pred_df = one_study_user_stacking2(
                result_df.copy().rename(columns={f"fold{fold}:b3_non_weight___pe_present_on_image":"pred0"}), 
                lgb_models, features)
            _res += inv_sigmoid( _df['stacking_pred'].values ) / len(FOLDS)
        df_sub.loc[stacking_pred_df['SOPInstanceUID'], 'label'] = sigmoid(_res)

        # _res = 0.0
        # STACK_TARGETS = ["pos_exam"] + list( lgb_models_each_target.keys() )
        STACK_TARGETS = list( lgb_models_each_target.keys() )
        _res_dict = {}
        for key in STACK_TARGETS:
            _res_dict[key] = 0.0

        for fold in FOLDS:
            one_study_test = result_df.copy().rename(columns={f"fold{fold}:b3_non_weight___pe_present_on_image" : "pred"})
            one_study_test['StudyInstanceUID'] = study
            test_grouped = grouping(one_study_test)
            # add monai feature
            print("<=>", study, monai_result_arr[fold])
            for key in [ 'chronic_pe', 'acute_and_chronic_pe', 'central_pe', 'leftsided_pe', 'rightsided_pe', 'rv_lv_ratio_gte_1']:
                test_grouped[ "monai_" + key ] = monai_result_arr[fold][ "monai_" + key ]
                # test_grouped[ "monai_" + key ] = monai_result.loc[ "monai_" + key ]
            # add position-b0 feature
            for col in ['pe_present_on_image', 'rightsided_pe', 'leftsided_pe', 'central_pe']:
                newkey = "position_model_" + col
                tmp_df = result_df.copy().rename(columns={f"fold{fold}:position___{col}"  : newkey})
                test_grouped = test_grouped.merge( grouping_one(tmp_df, newkey) , on="StudyInstanceUID")

            print("test_grouped", test_grouped)
                
            # pred_prob = one_study_user_stacking_posexam3(
            #     test_grouped.copy(),
            #     lgb_models_posexam, features_posexam)
            # _res_dict["pos_exam"] += inv_sigmoid( pred_prob ) / len(FOLDS)
            

            for key in lgb_models_each_target.keys():
                pred_prob = one_study_user_stacking_posexam3(
                        test_grouped.copy(),
                        lgb_models_each_target[key], features_exams_monai)
                _res_dict[key] += inv_sigmoid( pred_prob ) / len(FOLDS)

            # _res += inv_sigmoid( stacking_pred_posexam ) / len(FOLDS)
        for key in STACK_TARGETS:
            _res_dict[key] = sigmoid( _res_dict[key] )


        # posexam stacking
        # pos_exam_prob = _res_dict["pos_exam"]
        pos_exam_prob = _res_dict["positive_exam_for_pe"]

        ### fill exam_type (negative, indeterminate, positive) ### 
        # indeterminate_prob = _MEANS['indeterminate']  # from average
        ### df_sub.loc[study + '_' + 'indeterminate', 'label'] = indeterminate_prob
        df_sub.loc[study + '_' + 'indeterminate', 'label'] = (1 - pos_exam_prob) * (157) / (4911 + 157)

        df_sub.loc[study + '_' + 'negative_exam_for_pe', 'label'] = (1 - pos_exam_prob) * (4911) / (4911 + 157)
        print("==> ", study, pos_exam_prob, df_sub.loc[study + '_' + 'negative_exam_for_pe', 'label'])

        # ### fill right,left,central
        # for k in ['leftsided_pe', 'rightsided_pe', 'central_pe']:
        #     df_sub.loc[study + '_' + k, 'label'] = pos_exam_prob * _MEANS_POS[k] + (1 - pos_exam_prob) * _MEANS_NOT_POS[k]
        for k in list( lgb_models_each_target.keys() ):
            if k == "positive_exam_for_pe": continue
            print ( "==> ", study + '_' + k, _res_dict[ k ])
            df_sub.loc[study + '_' + k, 'label'] = _res_dict[ k ]

        # fill by pos_prob*ave_pos+(1-pos_prob)*ave_not_pos
        # for k in ['rv_lv_ratio_gte_1', 'rv_lv_ratio_lt_1', 'chronic_pe', 'acute_and_chronic_pe']:
        for k in ['acute_and_chronic_pe']:  # this target is bad when using monai
            df_sub.loc[study + '_' + k, 'label'] = pos_exam_prob * _MEANS_POS[k] + (1 - pos_exam_prob) * _MEANS_NOT_POS[k]
        
        print("======> ", df_sub[df_sub.index.str.contains(study)])


    df_sub.reset_index(inplace=True)
    df_sub.to_csv("submission.csv", index=False)


def sub(cfg, model, weight_path):
    utils.load_model(weight_path, model)
    model = model.eval()
    dataset_sub  = RsnaDatasetTest()

    df_test = pd.read_csv(DATADIR / "test.csv")
    result_all = {}
    for study in tqdm(df_test.StudyInstanceUID.unique()):
        dataset_sub.set_StudyInstanceUID(study)
        loader_sub = DataLoader(dataset_sub, batch_size=32, shuffle=False, pin_memory=True, num_workers=6)
        with torch.no_grad():
            result = run_nn(cfg, 'test', model, loader_sub)
            result_all[study] = result

    print("per study result's keys(): ", result_all[study].keys())
    return result_all


def sub_2(cfg, model, weight_path):
    utils.load_model(weight_path, model)
    result_all = {}
    model = model.eval()
    dataset_sub  = RsnaDatasetTest2()
    dataloader = DataLoader(dataset_sub, batch_size=1, shuffle=False, num_workers=3, collate_fn=lambda x:x)
    for (item) in tqdm(dataloader):
        imgs, study_id, sop_arr = item[0]
        _bs = 128
        preds = []
        for i in np.arange(0, len(sop_arr), step=_bs):
            _imgs = torch.from_numpy(imgs[i: i+_bs]).cuda()
            with torch.no_grad():
                outputs = model(_imgs)
            res = torch.sigmoid(outputs["pe_present_on_image"]).cpu().numpy()
            preds.extend(res)
        result_all[study_id] = {
            "outputs": np.array(preds),
            "ids": np.array(sop_arr),
        }
        if args.debug: break

    print("per study result's keys(): ", result_all[study_id].keys())
    # import pdb; pdb.set_trace()
    return result_all

# sub 3: pe_present + right,left,center
def sub_3(cfg, model, weight_path):
    """
    Returns: 
    result_all["study_id"] -> {
        "outputs" -> {"col_name1" -> np.ndarray, "col_name2 -> np.ndarray}
        "ids -> sop_id_arr
    }
    """
    utils.load_model(weight_path, model)
    result_all = {}
    model = model.eval()
    dataset_sub  = RsnaDatasetTest2()
    dataloader = DataLoader(dataset_sub, batch_size=1, shuffle=False, num_workers=1, collate_fn=lambda x:x)
    for (item) in tqdm(dataloader):
        imgs, study_id, sop_arr = item[0]
        _bs = 64
        outputs_all = defaultdict(list)
        for i in np.arange(0, len(sop_arr), step=_bs):
            _imgs = torch.from_numpy(imgs[i: i+_bs]).cuda()
            with torch.no_grad():
                outputs = model(_imgs)
            for _k in outputs.keys():  # iter over output keys:
                outputs_all[_k].extend(torch.sigmoid(outputs[_k]).cpu().numpy())  # currently all output is binarty logit
        result_all[study_id] = {
            "outputs": dict([(k, np.array(v)) for k, v in outputs_all.items()]),
            "ids": np.array(sop_arr),
        }
        if args.debug: break

    print("per study result's keys(): ", result_all[study_id].keys())
    return result_all

# almost same as sub_3. performance tuning and slightly difference return structure
# def sub_4(cfg, model, weight_path, valid_df=None):
def sub_4(cfg, img_models: dict, valid_df=None):
    """
    Returns: 
    result_all["study_id"] -> {
        "outputs" -> {"col_name1" -> np.ndarray, "col_name2 -> np.ndarray}
        "ids -> sop_id_arr
    }
    """

    # utils.load_model(weight_path, model)
    # model = model.eval()
    if valid_df is None:
        dataset_sub  = RsnaDatasetTest3()
    else:
        print("prepare dataset for local validation")
        dataset_sub  = RsnaDatasetTest3Valid(valid_df)

    dataloader = DataLoader(dataset_sub, batch_size=64, shuffle=False, num_workers=2)
    outputs_all = {modelname: defaultdict(list) for modelname,_ in img_models.items()}
    study_ids = []
    sop_ids = []
    z_positions = []
    debug_cnt = 0
    for imgs, study_arr, sop_arr, z_pos_arr in tqdm(dataloader):
        # import pdb; pdb.set_trace()
        imgs = imgs.cuda()
        with torch.no_grad():
            # for i in range(10): # simulate 10 models
            for modelname, (model,factor) in img_models.items():  # TODO do multi model inference
                outputs = model(imgs)

                outputs_all[modelname]["pe_present_on_image"].extend( calib_p( torch.sigmoid(outputs["pe_present_on_image"]).cpu().numpy(), factor) )
                for _k in ( outputs.keys() - ['pe_present_on_image']):  # iter over output keys:
                    outputs_all[modelname][_k].extend(torch.sigmoid(outputs[_k]).cpu().numpy())  # currently all output is binarty logit
        study_ids.extend(study_arr)
        sop_ids.extend(sop_arr)
        z_positions.extend(z_pos_arr)
        if args.debug:
            debug_cnt += 1
            if debug_cnt > 6: break

    # gather results per study
    ## all_output_keys = outputs_all.keys()
    study_ids = np.array(study_ids)
    sop_ids = np.array(sop_ids)
    z_positions = np.array(z_positions)
    result_final = {}
    for study in np.unique(study_ids):
        indice = study_ids == study
        sops = sop_ids[indice]
        z_pos = z_positions[indice]
        result_final[study] = {
            **{
                modelname: 
                    dict([(key, np.array(arr)[indice]) for key, arr in output_dict.items() ]) 
                for modelname, output_dict in outputs_all.items()
            },
            "ids": sops,
            'z_pos': z_pos
        }

    print("per study result's keys(): ", result_final[study].keys())

    # import pdb; pdb.set_trace();
    return result_final

# forked from sub_2, img-level+study-level
def sub_5(cfg, img_models, monai_models, df_test):
    """
    Returns: result_df_dict["study_id"] -> DataFrame
    """
    result_df_dict = {}
    monai_result_dict = {}  # MONAI
    monai_targets = [
        'monai_rv_lv_ratio_gte_1',
        "monai_central_pe",
        "monai_leftsided_pe",
        "monai_rightsided_pe",
        "monai_acute_and_chronic_pe",
        "monai_chronic_pe"
    ]


    ### dataset_sub  = RsnaDatasetTest2()
    dataset_sub  = RsnaDatasetTest2( df_test )
    dataloader = DataLoader(dataset_sub, batch_size=1, shuffle=False, num_workers=2, collate_fn=lambda x:x, pin_memory=True)
    for (item) in tqdm(dataloader):
        # imgs, study_id, sop_arr, z_pos_arr = item[0]
        imgs_numpy, study_id, sop_arr, z_pos_arr = item[0]
        imgs = torch.from_numpy(imgs_numpy).cuda()
        _bs = 64   // 2 ########################################################## LOCAL ONLY
        if ( _bs != 64 ): print("=============================> WARNING ============== BatchSize IS NOT 64")
        outputs_all = defaultdict(list)
        ### img-level models
        for i in np.arange(0, len(sop_arr), step=_bs):
            ### _imgs = torch.from_numpy(imgs[i: i+_bs]).cuda()
            _imgs = imgs[i: i+_bs]
            with torch.no_grad():
                for modelname, (model, pe_factor) in img_models.items():
                    outputs = model(_imgs)
                    outputs_all[modelname+"___"+"pe_present_on_image"].extend( calib_p( torch.sigmoid(outputs["pe_present_on_image"]).cpu().numpy(), pe_factor) )
                    for _k in outputs.keys():  # iter over output keys:
                        if _k == "pe_present_on_image": continue
                        outputs_all[modelname+"___"+_k].extend(torch.sigmoid(outputs[_k]).cpu().numpy())  # currently all output is binarty logit
        
        # monai prediction
        monai_img = monaimodel.monai_preprocess(imgs_numpy)
        pred_monai_arr = []
        for mo_mod in monai_models:
            pred_monai = mo_mod.pred_monai(monai_img)
            pred_monai_arr.append(pred_monai)

        per_study_df = pd.DataFrame({"SOPInstanceUID": sop_arr, "z_pos": z_pos_arr, **outputs_all})
        result_df_dict[study_id] = per_study_df

        monai_result_dict[study_id] = [ dict(
            {"StudyInstanceUID": study_id},
            ** dict( zip( monai_targets,  pred_monai_one ) )
        ) for pred_monai_one in pred_monai_arr ]

        if args.debug: break

    # monai_df = pd.DataFrame(monai_result_arr)

    return result_df_dict, monai_result_dict

if __name__ == "__main__":
    main()
