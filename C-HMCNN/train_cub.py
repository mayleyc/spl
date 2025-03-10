import os
import datetime
import json
from time import perf_counter

import torch
import torch.nn as nn

from torch.utils.tensorboard import SummaryWriter

from sklearn.metrics import (
    precision_score, 
    average_precision_score, 
    hamming_loss, 
    jaccard_score
)
from sklearn.model_selection import train_test_split

# Circuit imports
import sys
sys.path.append(os.path.join(sys.path[0],'hmc-utils'))
sys.path.append(os.path.join(sys.path[0],'hmc-utils', 'pypsdd'))

from GatingFunction import DenseGatingFunction
from compute_mpe import CircuitMPE
from pysdd.sdd import SddManager, Vtree

from sklearn import preprocessing

# misc
from common import *


def log1mexp(x):
        assert(torch.all(x >= 0))
        return torch.where(x < 0.6931471805599453094, torch.log(-torch.expm1(-x)), torch.log1p(-torch.exp(-x)))

from torch.utils.data import Dataset
from PIL import Image

class CUB_Dataset(Dataset):
    def __init__(self, image_paths, labels, transform = None, to_eval = True):
        """
        Args:
            image_paths (list): List of image file paths.
            transform (callable, optional): transform to be applied on a sample.
        """
        self.image_paths = image_paths
        self.labels = labels
        self.transform = transform
        self.to_eval = to_eval
    def __len__(self):
        """
        Returns dataset size.
        """

        return len(self.image_paths)

    def process_image(self, img_path):
        """
        Load, transform and pad an image.
        """
        # load image
        image_pil = Image.open(img_path).convert("RGB")  # load image

        if self.transform:
            image = self.transform(image_pil)  # 3, h, w
        else:
            transform = T.Compose(
            [
                T.Lambda(lambda img: resize_image(img, height=800, max_width=1333)),  # Resize with fixed height
                T.ToTensor(),
                T.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225]),
            ]
        )
            image = transform(image_pil)  # 3, H, W
        # Get current height and width
        _, H, W = image.shape

        # Compute padding
        pad_w = max(1333 - W, 0)  # Only pads if W < 1333
        pad_h = max(800 - H, 0)    # Only pads if H < 800

        # Compute symmetric padding
        pad_left = pad_w // 2
        pad_right = pad_w - pad_left
        pad_top = pad_h // 2
        pad_bottom = pad_h - pad_top

        # Apply padding correctly: (left, top, right, bottom)
        padded_image = F.pad(image, [pad_left, pad_top, pad_right, pad_bottom])  # Padding with 0s

        return image_pil, padded_image, padded_image.shape  #image = tensor
    
    def __getitem__(self, idx):
        """
        Get and process a sample given index `idx`.
        """
        img_path = self.image_paths[idx]
        label_set = self.labels[idx]
        _, image, _ = self.process_image(img_path)

        return image, label_set

class ConstrainedFFNNModel(nn.Module):
    """ C-HMCNN(h) model - during training it returns the not-constrained output that is then passed to MCLoss """
    def __init__(self, input_dim, hidden_dim, output_dim, hyperparams, R, dataset):
        super(ConstrainedFFNNModel, self).__init__()
        
        self.nb_layers = hyperparams['num_layers']
        self.R = R
        self.dataset = dataset
        if "cub" in self.dataset:
            self.conv1 = nn.Conv2d(3, 32, 3)
            self.conv2 = nn.Conv2d(32, 64, 3)

            self.pool = nn.MaxPool2d(2, 2) # Downsampling: reduce each dimension by half

            self.conv3 = nn.Conv2d(64, 128, 3)
            #self.conv4 = nn.Conv2d(128, 256, 3)

            #self.conv5 = nn.Conv2d(256, 512, 3)
            #self.conv6 = nn.Conv2d(512, 512, 3)

            # Adaptive Pooling
            self.global_pool = nn.AdaptiveAvgPool2d((7, 7)) # like resnet-50

        fc = []
        
        for i in range(self.nb_layers):
            if i == 0:
                if "cub" in dataset:
                    fc.append(nn.Linear(128 * 7 * 7, hidden_dim))
                else:
                    fc.append(nn.Linear(input_dim, hidden_dim))
            elif i == self.nb_layers-1:
                fc.append(nn.Linear(hidden_dim, output_dim))
            else:
                fc.append(nn.Linear(hidden_dim, hidden_dim))
        self.fc = nn.ModuleList(fc)
        
        self.drop = nn.Dropout(hyperparams['dropout'])
        
        if hyperparams['non_lin'] == 'tanh':
            self.f = nn.Tanh()
        else:
            self.f = nn.ReLU()
        
    def forward(self, x, sigmoid=False, log_sigmoid=False):
        if "cub" in self.dataset:
            x = self.pool(self.f(self.conv1(x)))
            x = self.pool(self.f(self.conv2(x)))
            
            x = self.pool(self.f(self.conv3(x)))
            #x = self.pool(self.f(self.conv4(x)))
            
            #x = self.pool(self.f(self.conv5(x)))
            #x = self.pool(self.f(self.conv6(x)))

            x = self.global_pool(x)
            x = torch.flatten(x, 1)  # Flatten for fully connected layers

        for i in range(self.nb_layers):
            if i == self.nb_layers-1:
                if sigmoid:
                    x = nn.Sigmoid()(self.fc[i](x))
                elif log_sigmoid:
                    x = nn.LogSigmoid()(self.fc[i](x))
                else:
                    x = self.fc[i](x)
            else:
                x = self.f(self.fc[i](x))
                x = self.drop(x)

        
        if self.R is None:
            return x
        
        if self.training:
            constrained_out = x
        else:
            constrained_out = get_constr_out(x, self.R)
        return constrained_out

def main():

    args = parse_args()

    # Set device
    torch.cuda.set_device(int(args.device))
    #print(torch.cuda.is_available())  # Should print True
    #print(torch.cuda.device_count())  # Should match the number of GPUs
    #print(torch.cuda.get_device_name(0))  # Check which GPU PyTorch is using

    # Load train, val and test set
    dataset_name = args.dataset
    data = dataset_name.split('_')[0]
    ontology = dataset_name.split('_')[1]
    hidden_dim = hidden_dims[ontology][data]

    num_epochs = args.n_epochs

    # Set the hyperparameters 
    hyperparams = {
        'num_layers': 3,
        'dropout': 0.7,
        'non_lin': 'relu',
    }

    # Set seed
    seed_all_rngs(args.seed)
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False

    device = torch.device("cuda:" + str(args.device) if torch.cuda.is_available() else "cpu")
    
    # Load the datasets

    import glob
    
    # list of files in cub 2011 and y labels
    if "cub" in args.dataset:
        #Try out with 5 classes in CUB
        # Get all class folder names
        all_classes = sorted(os.listdir(images_dir))  # Sorting ensures consistency

        # Select only the first 5 classes
        selected_classes = all_classes[:5]

        # Get image paths only for selected classes
        image_paths = []
        for cls in selected_classes:
            class_images = glob.glob(os.path.join(images_dir, cls, "*.jpg"))
            image_paths.extend(class_images)

        labels_unprocessed = [os.path.basename(os.path.dirname(path)) for path in image_paths]
        label_species = [label.split('.')[-1] for label in labels_unprocessed]
        label_species = [re.sub('_', ' ', label) for label in label_species] # the species-level label for each image
        # Create one-hot encoding based on species lookup in the csv
        ohe_dict = get_one_hot_labels(label_species, csv_path_mini)
        
        #print(ohe_dict)
        image_labels = [torch.from_numpy(ohe_dict[species]).to(device) for species in label_species]
        
        #Define image transform process
        transform = T.Compose(
                [
                    T.Lambda(lambda img: resize_image(img, height=800, max_width=1333)),  # Resize with fixed height
                    #T.Resize(800, max_size=1333), # like GroundingDINO
                    T.ToTensor(),
                    T.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225]),
                ]
            )
    if "cub" in args.dataset:    
        # Split dataset into train, val, and test sets
        train_paths, temp_paths, train_labels, temp_labels = train_test_split(image_paths, image_labels, test_size=0.3, random_state=args.seed)
        val_paths, test_paths, val_labels, test_labels = train_test_split(temp_paths, temp_labels, test_size=0.7, random_state=args.seed)
        
    elif ('others' in args.dataset):
        train, test = initialize_other_dataset(dataset_name, datasets)
        train.to_eval, test.to_eval = torch.tensor(train.to_eval, dtype=torch.bool),  torch.tensor(test.to_eval, dtype=torch.bool)
        train.X, valX, train.Y, valY = train_test_split(train.X, train.Y, test_size=0.30, random_state=args.seed)
    else:
        train, val, test = initialize_dataset(dataset_name, datasets)
        train.to_eval, val.to_eval, test.to_eval = torch.tensor(train.to_eval, dtype=torch.bool), torch.tensor(val.to_eval, dtype=torch.bool), torch.tensor(test.to_eval, dtype=torch.bool)
        print(train.Y.shape)

        #Create loaders
    if "cub" in args.dataset:
        # Create datasets for each split: Change labels
        train_dataset = CUB_Dataset(train_paths, train_labels, transform, to_eval = True)
        val_dataset = CUB_Dataset(val_paths, val_labels, transform, to_eval = True)
        test_dataset = CUB_Dataset(test_paths, test_labels, transform, to_eval = True)

        # convert them into tensors: shape = output_dim + 1
        train_dataset.to_eval, val_dataset.to_eval, test_dataset.to_eval = torch.tensor(train_dataset.to_eval, dtype=torch.bool), torch.tensor(val_dataset.to_eval, dtype=torch.bool), torch.tensor(test_dataset.to_eval, dtype=torch.bool)
    else:
        train_dataset = [(x, y) for (x, y) in zip(train.X, train.Y)]
        if ('others' not in args.dataset):
            val_dataset = [(x, y) for (x, y) in zip(val.X, val.Y)]
            #for (x, y) in zip(val.X, val.Y):
            #    train_dataset.append((x,y))
        else:
            val_dataset = [(x, y) for (x, y) in zip(valX, valY)]

        test_dataset = [(x, y) for (x, y) in zip(test.X, test.Y)]

    #different_from_0 = torch.tensor(np.array((test.Y.sum(0)!=0), dtype = bool), dtype=torch.bool)

    train_loader = torch.utils.data.DataLoader(dataset=train_dataset,
                                            batch_size=args.batch_size,
                                            shuffle=True)

    valid_loader = torch.utils.data.DataLoader(dataset=val_dataset,
                                            batch_size=args.batch_size,
                                            shuffle=False)

    test_loader = torch.utils.data.DataLoader(dataset=test_dataset,
                                            batch_size=args.batch_size,
                                            shuffle=False)

    # We do not evaluate the performance of the model on the 'roots' node (https://dtai.cs.kuleuven.be/clus/hmcdatasets/)
    if 'GO' in dataset_name:
        num_to_skip = 4
    else:
        num_to_skip = 1

    # Prepare matrix
    if "cub" in args.dataset:
        mat = np.load(mat_path_mini)
    else:
        mat = train.A

    # Prepare circuit: TODO needs cleaning
    if not args.no_constraints:
        print(mat.shape) #500x500 classes
        #print(np.array(train_labels).shape)
        
        if not os.path.isfile('constraints/' + dataset_name + '.sdd') or not os.path.isfile('constraints/' + dataset_name + '.vtree'):
            # Compute matrix of ancestors R
            # Given n classes, R is an (n x n) matrix where R_ij = 1 if class i is ancestor of class j
            #np.savetxt("foo.csv", mat, delimiter=",") #Check mat
            R = np.zeros(mat.shape)
            np.fill_diagonal(R, 1)
            g = nx.DiGraph(mat)
            for i in range(len(mat)):
                descendants = list(nx.descendants(g, i))
                if descendants:
                    R[i, descendants] = 1
            R = torch.tensor(R)

            #Transpose to get the ancestors for each node 
            R = R.unsqueeze(0).to(device)

            # Uncomment below to compile the constraint
            R.squeeze_()
            mgr = SddManager(
                var_count=R.size(0),
                auto_gc_and_minimize=True)

            alpha = mgr.true()
            alpha.ref()
            for i in range(R.size(0)):

               beta = mgr.true()
               beta.ref()
               for j in range(R.size(0)):

                   if R[i][j] and i != j:
                       old_beta = beta
                       beta = beta & mgr.vars[j+1]
                       beta.ref()
                       old_beta.deref()

               old_beta = beta
               beta = -mgr.vars[i+1] | beta
               beta.ref()
               old_beta.deref()

               old_alpha = alpha
               alpha = alpha & beta
               alpha.ref()
               old_alpha.deref()

            # Saving circuit & vtree to disk
            alpha.save(str.encode('constraints/' + dataset_name + '.sdd'))
            alpha.vtree().save(str.encode('constraints/' + dataset_name + '.vtree'))

        # Create circuit object
        cmpe = CircuitMPE('constraints/' + dataset_name + '.vtree', 'constraints/' + dataset_name + '.sdd')

        if args.S > 0:
            cmpe.overparameterize(S=args.S)
            print("Done overparameterizing")

        # Create gating function
        gate = DenseGatingFunction(cmpe.beta, gate_layers=[128] + [256]*args.gates, num_reps=args.num_reps).to(device)
        R = None


    else:
        # Use fully-factorized sdd
        mgr = SddManager(var_count=mat.shape[0], auto_gc_and_minimize=True)
        alpha = mgr.true()
        vtree = Vtree(var_count = mat.shape[0], var_order=list(range(1, mat.shape[0] + 1)))
        alpha.save(str.encode('ancestry.sdd'))
        vtree.save(str.encode('ancestry.vtree'))
        cmpe = CircuitMPE('ancestry.vtree', 'ancestry.sdd')
        cmpe.overparameterize()

        # Gating function
        gate = DenseGatingFunction(cmpe.beta, gate_layers=[462]).to(device)
        R = None

    # We do not evaluate the performance of the model on the 'roots' node (https://dtai.cs.kuleuven.be/clus/hmcdatasets/)
    if 'GO' in dataset_name: 
        num_to_skip = 4
    else:
        num_to_skip = 1 

    # Output path
    if args.exp_id:
        out_path = os.path.join(args.output, args.exp_id)
    else:
        date_string = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        out_path = os.path.join(args.output,  '{}_{}_{}_{}_{}'.format(args.dataset, date_string, args.batch_size, args.gates, args.lr))
    os.makedirs(out_path, exist_ok=True)

    # Tensorboard
    writer = SummaryWriter(log_dir=os.path.join(out_path, "runs"))

    # Dump experiment parameters
    args_out_path = os.path.join(out_path, 'args.json')
    json_args = json.dumps(vars(args))

    print("Starting with arguments:\n%s\n\tdumped at %s", json_args, args_out_path)
    with open(args_out_path, 'w') as f:
        f.write(json_args)

    # Create the model
    # Load train, val and test set

    model = ConstrainedFFNNModel(input_dims[data], hidden_dim, 128, hyperparams, R, args.dataset)
    model.to(device)
    print("Model on gpu", next(model.parameters()).is_cuda)
    optimizer = torch.optim.Adam(list(model.parameters()) + list(gate.parameters()), lr=args.lr, weight_decay=args.wd)
    criterion = nn.BCELoss(reduction="none")

    def evaluate(model):
        test_val_t = perf_counter()
        for i, (x,y) in enumerate(test_loader):

            model.eval()
                    
            x = x.to(device)
            y = y.to(device)

            constrained_output = model(x.float(), sigmoid=True)
            predicted = constrained_output.data > 0.5

            # Total number of labels
            total = y.size(0) * y.size(1)

            # Total correct predictions
            correct = (predicted == y.byte()).sum()
            num_correct = (predicted == y.byte()).all(dim=-1).sum()

            # Move output and label back to cpu to be processed by sklearn
            predicted = predicted.to('cpu')
            cpu_constrained_output = constrained_output.to('cpu')
            y = y.to('cpu')

            if i == 0:
                test_correct = num_correct
                predicted_test = predicted
                constr_test = cpu_constrained_output
                y_test = y
            else:
                test_correct += num_correct
                predicted_test = torch.cat((predicted_test, predicted), dim=0)
                constr_test = torch.cat((constr_test, cpu_constrained_output), dim=0)
                y_test = torch.cat((y_test, y), dim =0)
        
        if "cub" in args.dataset:
            test_cut = test_dataset.to_eval
        else:
            test_cut = test.to_eval
        '''print("test_cut, y_test.shape, y_test[:,test_cut].shape, constr_test.shape, constr_test.data[:,test_cut].shape")
        print(test_cut, y_test.shape, y_test[:,test_cut].shape, constr_test.shape, constr_test.data[:,test_cut].shape)
        print("predicted_test[:,test_cut].shape")
        print(predicted_test[:,test_cut].shape)'''
        
        test_val_e = perf_counter()
        avg_score = average_precision_score(y_test[:,test_cut], constr_test.data[:,test_cut], average='micro')
        jss = jaccard_score(y_test[:,test_cut], predicted_test[:,test_cut], average='micro')
        print(f"Number of correct: {test_correct}")
        print(f"avg_score: {avg_score}")
        print(f"test micro AP {jss}\t{(test_val_e-test_val_t):.4f}")

    def evaluate_circuit(model, gate, cmpe, epoch, data_loader, data_split, prefix):

        test_val_t = perf_counter()

        for i, (x,y) in enumerate(data_loader):

            model.eval()
            gate.eval()
                    
            x = x.to(device)
            y = y.to(device)

            # Parameterize circuit using nn
            emb = model(x.float())
            thetas = gate(emb)

            # negative log likelihood and map
            cmpe.set_params(thetas)
            nll = cmpe.cross_entropy(y, log_space=True).mean()

            cmpe.set_params(thetas)
            pred_y = (cmpe.get_mpe_inst(x.shape[0]) > 0).long()

            pred_y = pred_y.to('cpu')
            #print(pred_y.shape)
            y = y.to('cpu')
            #print(y.shape)

            num_correct = (pred_y == y.byte()).all(dim=-1).sum()

            if i == 0:
                test_correct = num_correct
                predicted_test = pred_y
                y_test = y
            else:
                test_correct += num_correct
                predicted_test = torch.cat((predicted_test, pred_y), dim=0)
                y_test = torch.cat((y_test, y), dim=0)

        dt = perf_counter() - test_val_t
        y_test = y_test[:,data_split.to_eval]
        predicted_test = predicted_test[:,data_split.to_eval]
        
        accuracy = test_correct / len(y_test)
        nll = nll.detach().to("cpu").numpy() / (i+1)
        '''if y_test.shape == predicted_test.shape:
            print("y_test.shape == predicted_test.shape")
        else:
            print("y_test and predicted_test shape mismatch")
        print(y_test.shape, predicted_test.shape)
        print(y_test.dtype, predicted_test.dtype)'''
        if "cub" in args.dataset:
            # Ensure correct shape (1D numpy array)
            y_test = y_test.squeeze()
            predicted_test = predicted_test.squeeze()
            # Convert to numpy (currently torch.int64)
            y_test = y_test.cpu().numpy()
            predicted_test = predicted_test.cpu().numpy()

        jaccard = jaccard_score(y_test, predicted_test, average='micro')
        hamming = hamming_loss(y_test, predicted_test)

        print(f"Evaluation metrics on {prefix} \t {dt:.4f}")
        print(f"Num. correct: {test_correct}")
        print(f"Accuracy: {accuracy}")
        print(f"Hamming Loss: {hamming}")
        print(f"Jaccard Score: {jaccard}")
        print(f"nll: {nll}")


        return {
            f"{prefix}/accuracy": (accuracy, epoch, dt),
            f"{prefix}/hamming": (hamming, epoch, dt),
            f"{prefix}/jaccard": (jaccard, epoch, dt),
            f"{prefix}/nll": (nll, epoch, dt),
        }

    if "cub" in args.dataset:
        data_split_test = test_dataset
        data_split_train = train_dataset
    else:
        data_split_test = test
        data_split_train = train

    for epoch in range(num_epochs):

        if epoch % 5 == 0 and epoch != 0:

            print(f"EVAL@{epoch}")
            perf = {
                **evaluate_circuit(
                    model,
                    gate, 
                    cmpe,
                    epoch=epoch,
                    data_loader=test_loader,
                    data_split=data_split_test,
                    prefix="param_sdd/test",
                ),
                **evaluate_circuit(
                    model,
                    gate,
                    cmpe,
                    epoch=epoch,
                    data_loader=valid_loader,
                    data_split=data_split_train,
                    prefix="param_sdd/valid",
                ),
            }

            for perf_name, (score, epoch, dt) in perf.items():
                writer.add_scalar(perf_name, score, global_step=epoch, walltime=dt)

            writer.flush()

        train_t = perf_counter()

        model.train()
        gate.train()

        tot_loss = 0
        for i, (x, labels) in enumerate(train_loader):

            x = x.to(device)
            labels = labels.to(device)
        
            # Clear gradients w.r.t. parameters
            optimizer.zero_grad()

            #MCLoss
            if args.no_constraints:

                # Use fully-factorized distribution via circuit
                output = model(x.float(), sigmoid=False)
                thetas = gate(output)
                cmpe.set_params(thetas)
                loss = cmpe.cross_entropy(labels, log_space=True).mean()

            else:
                y = labels
                output = model(x.float(), sigmoid=False)
                thetas = gate(output)
                cmpe.set_params(thetas)
                loss = cmpe.cross_entropy(labels, log_space=True).mean()

            tot_loss += loss
            loss.backward()
            optimizer.step()

        train_e = perf_counter()
        print(f"{epoch+1}/{num_epochs} train loss: {tot_loss/(i+1)}\t {(train_e-train_t):.4f}")

if __name__ == "__main__":
    main()
