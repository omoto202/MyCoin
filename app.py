import asyncio, json, time, hashlib, threading
from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO
import websockets
from ecdsa import SigningKey, VerifyingKey, SECP256k1


class Transaction:
    def __init__(self, sender, recipient, amount, signature=None):
        self.sender = sender
        self.recipient = recipient
        self.amount = amount
        self.signature = signature

    def __repr__(self):
        return f'{self.sender}->{self.recipient}:{self.amount}'

class Block:
    def __init__(self, transactions, previous_hash):
        self.timestamp = time.time()
        self.transactions = transactions
        self.previous_hash = previous_hash
        self.nonce = 0
        self.hash = self.calculate_hash()

    def calculate_hash(self):
        data = str(self.timestamp) + str(self.transactions) + self.previous_hash + str(self.nonce)
        return hashlib.sha256(data.encode()).hexdigest()

    def mine_block(self, difficulty):
        target = '0' * difficulty
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

    def verify_transaction(self, tx: Transaction):
        if tx.sender == "system":
            return True
        if not tx.signature:
            return False
        try:
            vk = VerifyingKey.from_string(bytes.fromhex(tx.sender), curve=SECP256k1)
            message = f"{tx.sender}->{tx.recipient}:{tx.amount}".encode()
            return vk.verify(bytes.fromhex(tx.signature), message)
        except:
            return False

    def create_transaction(self, tx: Transaction):
        if self.verify_transaction(tx):
            self.pending_transactions.append(tx)
            return True
        return False

    def mine_pending_transactions(self, miner_address):
        block = Block(self.pending_transactions, self.get_latest_block().hash)
        block.mine_block(self.difficulty)
        self.chain.append(block)
        self.pending_transactions = [Transaction("system", miner_address, self.mining_reward)]
        return block

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


peers = set()

async def broadcast(message):
    for peer in list(peers):
        try:
            async with websockets.connect(peer) as ws:
                await ws.send(json.dumps(message))
        except:
            pass

async def p2p_server(ws, path):
    async for msg in ws:
        data = json.loads(msg)
        if data['type'] == 'new_block':
            block_data = data['data']
            new_block = Block([], blockchain.get_latest_block().hash)
            new_block.timestamp = block_data['timestamp']
            new_block.hash = block_data['hash']
            blockchain.chain.append(new_block)
            socketio.emit('update', {'type':'block','hash':new_block.hash})
        elif data['type'] == 'new_tx':
            tx_data = data['data']
            tx = Transaction(tx_data['sender'], tx_data['recipient'], tx_data['amount'], tx_data.get('signature'))
            blockchain.create_transaction(tx)
            socketio.emit('update', {'type':'transaction'})

async def run_p2p_server(port=6000):
    server = await websockets.serve(p2p_server, "0.0.0.0", port)
    await server.wait_closed()

def start_p2p_in_thread(port=6000):
    threading.Thread(target=lambda: asyncio.run(run_p2p_server(port)), daemon=True).start()


@app.route('/')
def index():
    return render_template('index.html')

@app.route('/transactions/new', methods=['POST'])
def new_transaction():
    values = request.get_json()
    tx = Transaction(values['sender'], values['recipient'], values['amount'], values.get('signature'))
    if blockchain.create_transaction(tx):
        asyncio.run(broadcast({'type':'new_tx','data':values}))
        socketio.emit('update', {'type':'transaction'})
        return 'Transaction added', 201
    else:
        return 'Invalid signature', 400

@app.route('/mine', methods=['GET'])
def mine():
    miner_address = request.args.get('miner','anonymous')
    new_block = blockchain.mine_pending_transactions(miner_address)
    asyncio.run(broadcast({'type':'new_block','data':{
        'timestamp': new_block.timestamp,
        'hash': new_block.hash
    }}))
    socketio.emit('update', {'type':'block','hash':new_block.hash})
    return 'Block mined', 200

@app.route('/balance/<address>')
def balance(address):
    return jsonify({'balance': blockchain.get_balance(address)})

@app.route('/chain')
def full_chain():
    chain_data = []
    for block in blockchain.chain:
        chain_data.append({
            'timestamp': block.timestamp,
            'transactions':[{'sender':tx.sender,'recipient':tx.recipient,'amount':tx.amount,'signature':tx.signature} for tx in block.transactions],
            'hash': block.hash,
            'prev_hash': block.previous_hash,
            'nonce': block.nonce
        })
    return jsonify({'length': len(chain_data), 'chain': chain_data})


if __name__ == '__main__':
    start_p2p_in_thread(port=6000)
    socketio.run(app, port=5000)
