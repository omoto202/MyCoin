from flask import Flask, request, jsonify, render_template
from flask_socketio import SocketIO, emit
import time, hashlib, json, base64
from ecdsa import VerifyingKey, SECP256k1, BadSignatureError

class Transaction:
    def __init__(self, sender, recipient, amount, signature=None):
        self.sender = sender
        self.recipient = recipient
        self.amount = amount
        self.signature = signature

    def to_dict(self):
        return {
            'sender': self.sender,
            'recipient': self.recipient,
            'amount': self.amount
        }

    def calculate_hash(self):
        tx_str = json.dumps(self.to_dict(), sort_keys=True)
        return hashlib.sha256(tx_str.encode()).hexdigest()

    def is_valid(self):
        if self.sender == "system":
            return True
        if not self.signature or not self.sender:
            return False
        try:
            public_key_bytes = bytes.fromhex(self.sender)
            verifying_key = VerifyingKey.from_string(public_key_bytes, curve=SECP256k1)
            tx_hash = self.calculate_hash()
            signature_bytes = base64.b64decode(self.signature)
            verifying_key.verify(signature_bytes, tx_hash.encode())
            return True
        except BadSignatureError:
            return False
        except Exception as e:
            print("Signature verification error:", e)
            return False

class Block:
    def __init__(self, transactions, previous_hash):
        self.timestamp = time.time()
        self.transactions = transactions
        self.previous_hash = previous_hash
        self.nonce = 0
        self.hash = self.calculate_hash()

    def calculate_hash(self):
        data = str(self.timestamp) + json.dumps([t.to_dict() for t in self.transactions], sort_keys=True) + self.previous_hash + str(self.nonce)
        return hashlib.sha256(data.encode()).hexdigest()

    def mine_block(self, difficulty):
        target = "0" * difficulty
        while self.hash[:difficulty] != target:
            self.nonce += 1
            self.hash = self.calculate_hash()

class Blockchain:
    def __init__(self):
        self.chain = [self.create_genesis_block()]
        self.difficulty = 3
        self.pending_transactions = []
        self.mining_reward = 10

    def create_genesis_block(self):
        return Block([], "0")

    def get_latest_block(self):
        return self.chain[-1]

    def create_transaction(self, tx):
        if not tx.is_valid():
            raise Exception("Invalid transaction signature")
        self.pending_transactions.append(tx)

    def mine_pending_transactions(self, miner_address):
        block = Block(self.pending_transactions, self.get_latest_block().hash)
        block.mine_block(self.difficulty)
        self.chain.append(block)
        print(f"Block mined by {miner_address}")
        self.pending_transactions = [Transaction("system", miner_address, self.mining_reward)]

    def get_balance(self, address):
        balance = 0
        for block in self.chain:
            for tx in block.transactions:
                if tx.sender == address:
                    balance -= tx.amount
                if tx.recipient == address:
                    balance += tx.amount
        return balance


app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*")

blockchain = Blockchain()

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/transactions/new', methods=['POST'])
def new_transaction():
    values = request.get_json()
    required = ['sender', 'recipient', 'amount', 'signature']
    if not all(k in values for k in required):
        return 'Missing values', 400

    tx = Transaction(values['sender'], values['recipient'], values['amount'], values['signature'])

    if not tx.is_valid():
        return 'Invalid signature', 400

    blockchain.create_transaction(tx)
    socketio.emit('new_transaction', tx.to_dict())
    return 'Transaction added to pending pool', 201

@app.route('/mine', methods=['POST'])
def mine():
    values = request.get_json()
    miner_address = values.get('miner')
    if not miner_address:
        return "Missing miner address", 400

    blockchain.mine_pending_transactions(miner_address)
    socketio.emit('new_block', {'miner': miner_address})
    return jsonify({
        "message": "Block mined successfully!",
        "miner": miner_address,
        "balance": blockchain.get_balance(miner_address)
    }), 200

@app.route('/balance/<address>', methods=['GET'])
def balance(address):
    amount = blockchain.get_balance(address)
    return jsonify({'balance': amount})

@app.route('/chain', methods=['GET'])
def full_chain():
    chain_data = []
    for block in blockchain.chain:
        chain_data.append({
            'timestamp': block.timestamp,
            'transactions': [tx.to_dict() for tx in block.transactions],
            'hash': block.hash,
            'prev_hash': block.previous_hash,
            'nonce': block.nonce
        })
    return jsonify({'length': len(chain_data), 'chain': chain_data})

if __name__ == '__main__':
    socketio.run(app, port=5000, debug=True)
