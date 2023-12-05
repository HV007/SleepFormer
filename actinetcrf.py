# %load_ext autoreload
# %autoreload 2

import datetime
import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import Dataset
from torch.utils.data import DataLoader, random_split
import torch.nn.functional as F
from torchcrf import CRF
import numpy as np
import pandas as pd
from tqdm import tqdm
from sklearn.metrics import precision_score, recall_score, f1_score


class SleepDataset(Dataset):
    def __init__(self, parquet_file, sequence_length, is_test=False):
        self.data = pd.read_parquet(parquet_file)
        self.is_test = is_test
        self.sequence_length = sequence_length
        self.series_ids = self.data['series_id'].unique()
        self.data_chunks = self.preprocess_data()

    def preprocess_data(self):
        data_chunks = []
        for series_id in self.series_ids:
            series_data = self.data[self.data['series_id'] == series_id]
            data = series_data[['anglez', 'enmo']].values.astype(np.float32)
            try:
                timestamp = pd.to_datetime(series_data['timestamp'], utc=True).view('float').values / 10 ** 9
            except:
                breakpoint()
            step = series_data['step'].values.astype(np.float32)

            # Divide the time series into equal-sized chunks
            for i in range(0, len(data), self.sequence_length):
                chunk_data = data[i:i + self.sequence_length, :]
                chunk_timestamp = timestamp[i:i + self.sequence_length]
                chunk_step = step[i:i + self.sequence_length]

                if len(chunk_data) == self.sequence_length:  # Only include if the chunk is of sufficient length
                    if not self.is_test:
                        label = series_data['label'].values[i:i + self.sequence_length].astype(float)
                        data_chunks.append({'series_id': series_id, 'step': chunk_step, 'timestamp': chunk_timestamp, 'data': chunk_data, 'label': label})
                    else:
                        data_chunks.append({'series_id': series_id, 'step': chunk_step, 'timestamp': chunk_timestamp, 'data': chunk_data})

        return data_chunks

    def __len__(self):
        return len(self.data_chunks)

    def __getitem__(self, idx):
        return self.data_chunks[idx]


class ActiNetCRFModel(nn.Module):
    def __init__(self, input_size = 2, hidden_size = 64, num_classes = 2, dropout = 0.25):
        super(ActiNetCRFModel, self).__init__()
        self.name = 'ActiNetCRFv1'
        self.num_classes = num_classes
        self.model = nn.Sequential(
            nn.Conv1d(in_channels=input_size, out_channels=hidden_size, kernel_size=1, padding = 'same'),
            nn.GELU(),
            nn.Dropout(dropout),
            nn.BatchNorm1d(hidden_size),
            nn.Conv1d(in_channels=hidden_size, out_channels=hidden_size, kernel_size=1, padding = 'same'),
            nn.GELU(),
            nn.Dropout(dropout),
            nn.BatchNorm1d(hidden_size),
            nn.Conv1d(in_channels=hidden_size, out_channels=hidden_size, kernel_size=1, padding = 'same'),
            nn.GELU(),
            nn.Dropout(dropout),
            nn.BatchNorm1d(hidden_size),
            nn.Conv1d(in_channels=hidden_size, out_channels=hidden_size, kernel_size=1, padding = 'same'),
            nn.GELU(),
            nn.Dropout(dropout),
            nn.BatchNorm1d(hidden_size)
        )
        self.lstm = nn.LSTM(input_size=hidden_size, hidden_size=hidden_size,batch_first=True, bidirectional = True)
        self.fc = nn.Linear(2 * hidden_size, num_classes)
        self.crf = CRF(num_classes)

    def forward(self, x, tags = None):
        x = x.transpose(1, 2)
        x = self.model(x)
        x = x.transpose(1, 2).contiguous()
        x, _ = self.lstm(x)
        logits = self.fc(x)
        if tags is not None:
            output = -self.crf(logits, tags)
        else :
            output = self.crf.decode(logits)
        return output


def train_model(model, data_loader, criterion, optimizer):
    model.train()
    total_loss = 0
    device = 'cuda' if torch.cuda.is_available() else 'cpu'

    # Wrap the DataLoader with tqdm to display a progress bar
    for batch in tqdm(data_loader, leave=False):
        # Extract data from the batch
        input_data = batch['data'].to(device)
        labels = batch['label'].type(torch.LongTensor).to(device)

        # Clear the previous gradients
        optimizer.zero_grad()

        # Forward pass
        loss = model(input_data, tags = labels)

        # Backward pass and optimization
        loss.backward()
        optimizer.step()

        total_loss += loss.item()

    # Calculate accuracy and print during training
    average_loss = total_loss / len(data_loader)
    return average_loss


def test_model(model, test_loader, criterion):
    model.eval()
    correct_predictions = 0
    total_samples = 0
    all_preds = []
    all_labels = []
    device = 'cuda' if torch.cuda.is_available() else 'cpu'

    with torch.no_grad():
        for batch in tqdm(test_loader, leave=False):
            input_data = batch['data'].to(device)
            labels = batch['label'].type(torch.LongTensor).to(device)

            logits = model(input_data)
            logits = torch.tensor(np.array(logits).T).contiguous().to(device)

            predicted = logits.view(-1)
            labels_flat = labels.view(-1)

            correct_predictions += torch.sum(predicted == labels_flat).item()
            total_samples += labels.numel()

            all_preds.extend(predicted.view(-1).cpu().numpy())
            all_labels.extend(labels_flat.cpu().numpy())
    
    # Calculate validation metrics
    accuracy = correct_predictions / total_samples

    precision = precision_score(all_labels, all_preds, average='weighted')
    recall = recall_score(all_labels, all_preds, average='weighted')
    f1 = f1_score(all_labels, all_preds, average='weighted')

    return accuracy, precision, recall, f1


def predict_events(model, test_loader):
    model.eval()

    # Make predictions on the test dataset
    predictions = []
    series_ids = []
    steps = []  
    with torch.no_grad():
        for batch in tqdm(test_loader):
        
            input_data = batch['data'].to(device)


            # Forward pass
            logits = model(input_data)

            # Convert logits to predictions
            _, predicted = torch.max(logits, 2)
            predictions.extend(predicted.cpu().numpy()[0])
            series_ids.extend(np.full(len(predicted.cpu().numpy()[0]), batch['series_id'][0]))
            steps.extend(batch['step'].cpu().numpy()[0])
            
    pred_dict = pred_to_dict(series_ids, steps, predictions)
    pred_dict = remove_outliers(pred_dict)
    pred_dict = get_local_best(pred_dict)

    prediction_rows = []
    curr_state = None
    curr_series = None
    for series_id, series_dict in tqdm(pred_dict.items()):
        curr_state = series_dict["preds"][0]
        for pred, step in zip(series_dict["preds"][1:],  series_dict["steps"][1:]):
            
            if pred != curr_state:
                curr_state = pred
                event = 'wakeup' if curr_state == 1 else 'onset'
                prediction_rows.append({
                    'row_id': len(prediction_rows),
                    'series_id': curr_series,
                    'step': int(step),
                    'event': event,
                    'score': 1.0
                })
    
    return pd.DataFrame(prediction_rows)

# Example usage for training DataLoader
device = 'cuda' if torch.cuda.is_available() else 'cpu'
batch_size = 8  # Adjust based on your needs
sequence_length = 250  # Adjust based on your needs

print('Loading Dataset')
dataset = SleepDataset(parquet_file='dataset/combined_0.parquet', sequence_length=sequence_length, is_test=False)
print('Dataset Loaded')

# Split the dataset into training and validation sets (80:20)
train_size = int(0.8 * len(dataset))
val_size = len(dataset) - train_size
train_dataset, val_dataset = random_split(dataset, [train_size, val_size])

train_loader = DataLoader(
    dataset=train_dataset,
    batch_size=batch_size,
    shuffle=True,
    num_workers=2,  # Adjust based on your system capabilities
)

val_loader = DataLoader(
    dataset=val_dataset,
    batch_size=batch_size,
    shuffle=True,
    num_workers=2,  # Adjust based on your system capabilities
)

# Example usage
input_size = 2  # Assuming 2 features in the input (anglez and enmo)
hidden_size = 64
num_classes = 2  # Number of classes: asleep and awake
dropout = 0.15
learning_rate = 0.001
num_epochs = 10

# Initialize the model
model = ActiNetCRFModel(input_size, hidden_size, num_classes, dropout).to(device)
criterion = nn.CrossEntropyLoss()
optimizer = optim.Adam(model.parameters(), lr=learning_rate)

# +
timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

log = open('logs/' + model.name + f'{timestamp}' + '.log', 'w')
# -

# Train the model
print('Starting Model Training')
best_f1 = 0.0
for epoch in range(num_epochs):
    loss = train_model(model, train_loader, criterion, optimizer)
    print(f'Epoch [{epoch+1}/{num_epochs}], Loss: {loss:.4f}')
    log.write(f'Epoch [{epoch+1}/{num_epochs}], Loss: {loss:.4f}\n')
    accuracy, precision, recall, f1 = test_model(model, val_loader, criterion)
    print(f'Validation Accuracy: {accuracy * 100:.2f}%, Precision: {precision:.4f}, Recall: {recall:.4f}, F1: {f1:.4f}')
    log.write(f'Validation Accuracy: {accuracy * 100:.2f}%, Precision: {precision:.4f}, Recall: {recall:.4f}, F1: {f1:.4f}\n')
    if f1 > best_f1:
        best_f1 = f1
        torch.save(model.state_dict(), 'models/' + model.name + '.pth')
        print('Model Saved')

log.close()

#remove this cell before submitting
model.load_state_dict(torch.load())
print("model loaded")
test_dataset = SleepDataset(parquet_file='dataset/combined_3.parquet', sequence_length=300, is_test=True)
test_loader = DataLoader(dataset=test_dataset, batch_size=1, shuffle=False, num_workers=1)
print("test_dataset loaded")

events = predict_events(model, test_loader)
events

