import sys
import os
import argparse
import random
import torch
import numpy as np
import networkx as nx

import torchvision.transforms as T
import torchvision.transforms.functional as F

import os
import re
from PIL import Image

from tqdm import tqdm

import pandas as pd

from sklearn.impute import SimpleImputer
from sklearn.preprocessing import StandardScaler


sys.path.append(os.path.join(sys.path[0], "."))
os.environ["DATA_FOLDER"] = "."
from cutils.parser import *
from cutils import datasets
csv_path = "CUB/bird_info.csv"
csv_path_mini = "CUB/bird_info_mini.csv"
mat_path = "cub_matrix.npy"
mat_path_mini = "cub_matrix_mini.npy"
images_dir = "CUB/CUB_200_2011/images"

input_dims = {
    "diatoms": 371,
    "enron": 1001,
    "imclef07a": 80,
    "imclef07d": 80,
    "cellcycle": 77,
    "derisi": 63,
    "eisen": 79,
    "expr": 561,
    "gasch1": 173,
    "gasch2": 52,
    "seq": 529,
    "spo": 86,
    "cub": 1333 #3199200,
}

output_dims_FUN = {
    "cellcycle": 499,
    "derisi": 499,
    "eisen": 461,
    "expr": 499,
    "gasch1": 499,
    "gasch2": 499,
    "seq": 499,
    "spo": 499,
}

output_dims_GO = {
    "cellcycle": 4122,
    "derisi": 4116,
    "eisen": 3570,
    "expr": 4128,
    "gasch1": 4122,
    "gasch2": 4128,
    "seq": 4130,
    "spo": 4116,
}

output_dims_others = {
    "diatoms": 398,
    "enron": 56,
    "imclef07a": 96,
    "imclef07d": 46,
    "reuters": 102,
    "cub": 5, #200
}

output_dims = {
    "FUN": output_dims_FUN,
    "GO": output_dims_GO,
    "others": output_dims_others,
}

hidden_dims_FUN = {
    "cellcycle": 500,
    "derisi": 500,
    "eisen": 500,
    "expr": 1250,
    "gasch1": 1000,
    "gasch2": 500,
    "seq": 2000,
    "spo": 250,
}

hidden_dims_GO = {
    "cellcycle": 1000,
    "derisi": 500,
    "eisen": 500,
    "expr": 4000,
    "gasch1": 500,
    "gasch2": 500,
    "seq": 9000,
    "spo": 500,
}

hidden_dims_others = {
    "diatoms": 2000,
    "enron": 1000,
    "imclef07a": 1000,
    "imclef07d": 1000,
    "cub": 1000, #should be a hyperparameter? check for best performance
}

hidden_dims = {
    "FUN": hidden_dims_FUN,
    "GO": hidden_dims_GO,
    "others": hidden_dims_others,
}


def seed_all_rngs(seed):
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    os.environ["PYTHONHASHSEED"] = str(seed)

def is_descendant(df, val1, val2):
    for _, row in df.iterrows(): #iterate through each row
        if val1 in row.values and val2 in row.values:
            val1_idx = row.index.get_loc(row[row == val1].index[0])  #trace up to compare the position
            val2_idx = row.index.get_loc(row[row == val2].index[0])
            if val1_idx is not None and val2_idx is not None and val1_idx < val2_idx:
                return True
    return False

# convert the bird info csv into train.A
def csv_2_matrix(df):
    unique_values = []
    for column_name in df.columns[-4:]:  # Focus on last 4 columns
        uv_col_list = df[column_name].dropna().unique().tolist()  # Extract unique values
        unique_values += uv_col_list
    #return unique_values
    mat = []
    print("Building the matrix from unique values...")
    for i in tqdm(unique_values):
        for j in unique_values:
            if is_descendant(df, i, j):
                mat.append(1)
            else:
                mat.append(0)
    
    mat = np.array(mat).reshape(len(unique_values), len(unique_values))
    return mat

def resize_image(image, height=800, max_width=1333):
        """
        Resize the image while maintaining the aspect ratio:
        - The **height** is fixed at `fixed_height` (800).
        - The **width** is scaled proportionally and capped at `max_width` (1333).
        """
        W, H = image.size  # Get original width & height

        # Compute the scale to match the fixed height
        scale = height / H
        new_H, new_W = height, int(W * scale)

        # Ensure the width does not exceed the max_width
        if new_W > max_width:
            scale = max_width / new_W
            new_H, new_W = int(new_H * scale), int(new_W * scale)

        return F.resize(image, (new_H, new_W))

def get_one_hot_labels(label_species: list, csv_path: str):
    label_dict = {}
    df = pd.read_csv(csv_path)
    for i in label_species:
        labels = []
        row_idx, _ = np.where(df == i)
        labels = df.loc[row_idx, df.columns[-4:]]
        label_dict[i] = labels.values.tolist()[0] # converts to list and remove the outer list
    #print(label_dict)
    # Convert label_dict to tensors    
    unique_values = []
    for column_name in df.columns[-4:]:  # Focus on last 4 columns, from left to right
        uv_col_list = df[column_name].dropna().unique().tolist()  # Extract unique values
        unique_values += uv_col_list
    # transform it into an index map for faster lookup
    unique_val_map = {value:idx for idx, value in enumerate(unique_values)}
    # from label_dict, create one-hot encoding for each label
    ohe_dict = {}
    for i in label_dict:
        for j in label_dict[i]:
            array = np.zeros(len(unique_values), dtype=int)
            idx = unique_val_map.get(j)
            if idx is not None:
                array[idx] += 1
        ohe_dict[i] = array

    return ohe_dict

def get_data_and_loaders(dataset_name, batch_size, device):


    train, val, test = initialize_dataset(dataset_name, datasets)

    # XXX einet dies unless we use validation here in, e.g., eisen
    preproc_X = (
        train.X if val is None else np.concatenate((train.X, val.X))
    ).astype(float)
    scaler = StandardScaler().fit(preproc_X)
    imputer = SimpleImputer(
        missing_values=np.nan,
        strategy='mean'
    ).fit(preproc_X)
    
    def process(dataset, shuffle=False):
        if dataset is None:
            return None
        assert np.all(np.isfinite(dataset.X))
        assert np.all(np.unique(dataset.Y.ravel()) == np.array([0, 1]))
        dataset.to_eval = torch.tensor(dataset.to_eval, dtype=torch.bool)
        dataset.X = torch.tensor(
            scaler.transform(imputer.transform(dataset.X))
        ).to(device)
        dataset.Y = torch.tensor(dataset.Y).to(device)
        loader = torch.utils.data.DataLoader(
            dataset=[(x, y) for (x, y) in zip(dataset.X, dataset.Y)],
            batch_size=batch_size,
            shuffle=shuffle
        )
        return loader

    train_loader = process(train, shuffle=True)
    valid_loader = process(val, shuffle=False)
    test_loader = process(test, shuffle=False)

    return train, train_loader, val, valid_loader, test, test_loader


def compute_ancestor_matrix(A, device, transpose=True, no_constraints=False):
    """Compute matrix of ancestors R.

    Given n classes, R is an (n x n) matrix where R_ij = 1 if class i is
    ancestor of class j.
    """
    if no_constraints:
        return None

    R = np.zeros(A.shape)
    np.fill_diagonal(R, 1)
    g = nx.DiGraph(A)
    for i in range(len(A)):
        descendants = list(nx.descendants(g, i))
        if descendants:
            R[i, descendants] = 1
    R = torch.tensor(R)
    if transpose:
        R = R.transpose(1, 0)
    R = R.unsqueeze(0).to(device)

    return R


def get_constr_out(x, R):
    """ Given the output of the neural network x returns the output of MCM given the hierarchy constraint expressed in the matrix R """

    if R is None:
        return x

    c_out = x.double()
    c_out = c_out.unsqueeze(1)
    c_out = c_out.expand(len(x), R.shape[1], R.shape[1])
    R_batch = R.expand(len(x), R.shape[1], R.shape[1])
    final_out, _ = torch.max(R_batch * c_out.double(), dim=2)
    return final_out


def parse_args():

    fmt_class = argparse.ArgumentDefaultsHelpFormatter
    parser = argparse.ArgumentParser(formatter_class=fmt_class)

    parser.add_argument(
        "--dataset",
        type=str,
        default=None,
        required=True,
        help='dataset name, must end with: "_GO", "_FUN", or "_others"',
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=1337,
        help="random seed")
    parser.add_argument(
        "--device",
        type=str,
        default="0",
        help="GPU"
    )
    parser.add_argument(
        "--emb-size",
        type=int,
        default=128,
        help="Embedding layer size"
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=100,
        help="Batch size"
    )
    parser.add_argument(
        "--lr",
        type=float,
        default=1e-4,
        help="Learning rate"
    )
    parser.add_argument(
        "--wd",
        type=float,
        default=1e-5,
        help="Weight decay"
    )
    parser.add_argument(
        "--n-epochs",
        type=int,
        default=200,
        help="Num epochs"
    )
    parser.add_argument(
        "-o",
        "--output",
        type=str,
        default="exp",
        help="Output path to exp result"
    )
    parser.add_argument(
        "--exp-id",
        type=str,
        default=None,
        help="Dataset output suffix"
    )
    parser.add_argument(
        "--no-constraints",
        action="store_true"
    )
    parser.add_argument(
        "--gates", 
        type=int, 
        default=1,
        help='Number of hidden layers in gating function (default: 1)'
    )
    parser.add_argument(
        "--S", 
        type=int, 
        default=0,
        help='PSDD scaling factor (default: 0)'
    )
    parser.add_argument(
        "--num_reps", 
        type=int, 
        default=1,
        help='Number of PSDDs in the ensemble'
    )

    args = parser.parse_args()

    assert "_" in args.dataset
    assert (
        "FUN" in args.dataset
        or "GO" in args.dataset
        or "others" in args.dataset
    )

    return args
