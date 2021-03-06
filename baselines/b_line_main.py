from helper import CDataset, get_embeddings, get_customer_embeddings, ensure_dir, get_time_mask
from torch.utils.data import DataLoader
import numpy as np
from baselines.models import SimpleGRU, StructuralRNN, NodeInterpolation, NodeNeighborsInterpolation, GAT, RNNGAT
import torch.optim as optim
from torch.autograd import Variable
import torch
import random

import argparse
import visdom
from datetime import datetime
from os import path

vis = visdom.Visdom(port=8097)
EXP_NAME = "exp-{}".format(datetime.now())

inv_softlog_1 = lambda x: (torch.exp(x) - 2)
inv_softlog = lambda x: (torch.exp(x) - 1) * 10
inv_softplus = lambda x: torch.log(torch.exp(x) - 1)

def __pars_args__():
    parser = argparse.ArgumentParser(description='Guided attention model')
    parser.add_argument("--model", type=str, default="SimpleGRU", help="Directory containing dataset file")
    parser.add_argument("--data_dir", "-d_dir", type=str, default="pems", help="Directory containing dataset file")
    parser.add_argument("--dataset_prefix", type=str, default="", help="Prefix for the dataset")
    parser.add_argument("--train_file_name", "-train_fn", type=str, default="train_dataset", help="Train file name")
    parser.add_argument("--eval_file_name", "-eval_fn", type=str, default="eval_dataset", help="Eval file name")
    parser.add_argument("--test_file_name", "-test_fn", type=str, default="test_dataset", help="Test file name")

    parser.add_argument("--use_cuda", "-cuda", type=bool, default=False, help="Use cuda computation")
    parser.add_argument('--batch_size', type=int, default=100, help='Batch size for training.')
    parser.add_argument('--eval_batch_size', type=int, default=50, help='Batch size for eval.')

    parser.add_argument('--input_dim', type=int, default=54, help='Embedding size.')
    parser.add_argument('--hidden_size', type=int, default=128, help='Hidden state memory size.')
    parser.add_argument('--output_size', type=int, default=1, help='output size.')
    parser.add_argument('--drop_prob', type=float, default=0.1, help="Keep probability for dropout.")
    parser.add_argument('--time_windows', type=int, default=10, help='Attention time windows.')
    parser.add_argument('--max_neighbors', "-m_neig", type=int, default=6, help='Max number of neighbors.')

    parser.add_argument('-lr', '--learning_rate', type=float, default=0.001, help='learning rate (default: 0.001)')
    parser.add_argument('--epsilon', type=float, default=0.1, help='Epsilon value for Adam Optimizer.')
    parser.add_argument('--max_grad_norm', type=float, default=30.0, help="Clip gradients to this norm.")
    parser.add_argument('--n_iter', type=int, default=51, help="Iteration number.")
    parser.add_argument('--eval_step', type=int, default=10, help='How often do an eval step')
    parser.add_argument('--save_rate', type=float, default=0.9, help='How often do save an eval example')
    parser.add_argument('--device', type=int, default=0, help='GPU device')
    return parser.parse_args()


def setup_model(model, batch_size, args, is_training=True):
    if is_training:
        # optimizer = optim.Adagrad(model.parameters(), lr=args.learning_rate)
        optimizer = optim.Adam(model.parameters(), lr=args.learning_rate)
    else:
        optimizer = None

    def execute(dataset, input_embeddings, target_embeddings, neighbor_embeddings, edge_types, mask_neigh, device):
        _loss = 0
        saved_weights = {}

        for b_idx, b_index in enumerate(dataset):
            b_input_sequence = input_embeddings[b_index].to(device)
            b_target_sequence = target_embeddings[b_index].to(device)
            b_neighbors_sequence = neighbor_embeddings[b_index].to(device)
            b_edge_types = edge_types[b_index].to(device)
            b_mask_neigh = mask_neigh[b_index].to(device)
            b_mask_time = get_time_mask(args.time_windows, b_neighbors_sequence.size()).to(device)


            if args.model == "RNNGAT":
                node_hidden = [model.init_hidden(batch_size * (args.max_neighbors+1)).to(device) for i in range(4)]
                neighbor_hidden = None
            elif args.model == "StructuralRNN":
                node_hidden = model.init_hidden(batch_size).to(device)
                neighbor_hidden = model.init_hidden(batch_size).to(device)
            elif args.model == "SimpleGRU":
                node_hidden = model.init_hidden(batch_size).to(device)
                neighbor_hidden = None
            else:
                node_hidden = None
                neighbor_hidden = None



            if is_training:
                model.train()
                model.zero_grad()
                with torch.set_grad_enabled(True):
                    predict = model.forward(b_input_sequence,
                                            node_hidden,
                                            b_neighbors_sequence,
                                            neighbor_hidden,
                                            b_edge_types,
                                            b_mask_neigh,
                                            b_mask_time)

                    loss = model.compute_loss(predict.squeeze(), b_target_sequence.squeeze())
                    loss.backward()
                    torch.nn.utils.clip_grad_norm_(model.parameters(), args.max_grad_norm)
                    optimizer.step()


            else:
                model.eval()
                with torch.set_grad_enabled(False):
                    predict = model.forward(b_input_sequence,
                                            node_hidden,
                                            b_neighbors_sequence,
                                            neighbor_hidden,
                                            b_edge_types,
                                            b_mask_neigh,
                                            b_mask_time)

                if args.data_dir == "pems":
                    predict = inv_softlog(predict.squeeze())
                    target = inv_softlog(b_target_sequence.squeeze())
                elif args.data_dir == "utility":
                    predict = inv_softplus(predict.squeeze())
                    target = inv_softplus(b_target_sequence.squeeze())
                elif args.data_dir == "customers":
                    predict = inv_softlog_1(predict.squeeze())
                    target = inv_softlog_1(b_target_sequence.squeeze())
                else:
                    predict = predict.squeeze()
                    target = b_target_sequence.squeeze()

                loss = model.compute_error(predict, target)

            _loss += loss.item()
            b_idx += 1

            if (b_idx * args.batch_size) % 1000 == 0:
                print("num example:{}\tloss:{}".format((b_idx * args.batch_size), _loss / b_idx))

        _loss /= b_idx
        return _loss, saved_weights
    return execute

if __name__ == "__main__":
    args = __pars_args__()
    device = torch.device("cuda:{}".format(args.device) if args.use_cuda else "cpu")


    input_embeddings, target_embeddings, neighbor_embeddings, edge_types, mask_neighbor = get_embeddings(path.join("..", "data", args.data_dir), prefix=args.dataset_prefix)

    train_dataset = CDataset(path.join("..", "data", args.data_dir), "{}{}".format(args.dataset_prefix, args.train_file_name))
    eval_dataset = CDataset(path.join("..", "data", args.data_dir), "{}{}".format(args.dataset_prefix, args.eval_file_name))
    test_dataset = CDataset(path.join("..", "data", args.data_dir), "{}{}".format(args.dataset_prefix, args.test_file_name))

    train_dataloader = DataLoader(train_dataset, batch_size=args.batch_size, shuffle=True, num_workers=1,
                                  drop_last=True)
    eval_dataloader = DataLoader(eval_dataset, batch_size=args.eval_batch_size, shuffle=False, num_workers=1,
                                 drop_last=True)
    test_dataloader = DataLoader(test_dataset, batch_size=args.eval_batch_size, shuffle=False, num_workers=1,
                                 drop_last=True)





    if args.model == "GAT" or args.model == "SingleGAT" or args.model == "RNNGAT":
        model = eval(args.model)(args.input_dim, args.hidden_size, args.output_size, args.max_neighbors, dropout_prob=args.drop_prob)
    elif args.model == "StructuralRNN" or args.model == "NodeNeighborsInterpolation":
        model = eval(args.model)(args.input_dim, args.hidden_size, args.output_size, edge_types.size(-1), dropout_prob=args.drop_prob)
    else:
        model = eval(args.model)(args.input_dim, args.hidden_size, args.output_size, dropout_prob=args.drop_prob)

    test_rmse = []

    for i in range(2):
        model.reset_parameters()

        model = model.to(device)

        train = setup_model(model, args.batch_size, args, True)
        eval = setup_model(model, args.eval_batch_size, args, is_training=False)

        total_loss = []
        eval_number = 0
        eval_loss = []
        best_model = float("infinity")

        for i_iter in range(args.n_iter):
            iter_loss, _ = train(train_dataloader, input_embeddings, target_embeddings, neighbor_embeddings, edge_types, mask_neighbor, device)
            total_loss.append(iter_loss)
            print(iter_loss)

            # plot loss
            vis.line(
                Y=np.array(total_loss),
                X=np.array(range(i_iter + 1)),
                opts=dict(
                        legend=["loss"],
                        title=model.name + " training loos",
                        showlegend=True),
                win="win:train-{}".format(EXP_NAME))

            if i_iter % args.eval_step == 0:
                iter_eval, saved_weights = eval(eval_dataloader, input_embeddings, target_embeddings, neighbor_embeddings, edge_types, mask_neighbor, device)
                eval_loss.append(iter_eval)
                vis.line(
                    Y=np.array(eval_loss),
                    X=np.array(range(0, i_iter + 1, args.eval_step)),
                    opts=dict(legend=["RMSE"],
                              title=model.name + " eval loos",
                              showlegend=True),
                    win="win:eval-{}".format(EXP_NAME))

                # print("dump example")
                # torch.save(saved_weights, ensure_dir(path.join(path.join("..", "data", args.data_dir), model.name, "saved_eval_iter_{}_drop_{}.pt".format(int(i_iter/args.eval_step), args.drop_prob))))
                # print("dump done")

                if best_model > iter_eval:
                    print("save best model")
                    best_model = iter_eval
                    torch.save(model, path.join(path.join("..", "data", args.data_dir), "{}.pt".format(model.name)))


        # test performance
        model = torch.load(path.join(path.join("..", "data", args.data_dir), "{}.pt".format(model.name)))

        test = setup_model(model, args.eval_batch_size, args, is_training=False)
        iter_test, saved_weights = test(test_dataloader, input_embeddings, target_embeddings, neighbor_embeddings, edge_types, mask_neighbor, device)
        print("test RMSE: {}".format(iter_test))
        torch.save(saved_weights, ensure_dir(
            path.join(path.join("..", "data", args.data_dir), model.name, "saved_test_drop_{}.pt".format(args.drop_prob))))
        test_rmse.append(iter_test)
    print("execution_mean: {}".format(np.mean(test_rmse)))