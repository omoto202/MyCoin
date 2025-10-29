import asyncio, json, time, hashlib, threading
from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO
import websockets
from ecdsa import VerifyingKey, SECP256k1

# -------------------------
# Blockchain classes
# -------------------------
class Transaction:
    def __init__(self, sender, recipient, amount, signature=None):
        self.sender = sender
        self.recipient = recipient
        self.amount = amount
        self.signature = signature

    def to_dict(self):
        return {"sender": self.sender, "recipient": self.recipient, "amount": self.amount, "signature": self.signature}

    def __repr__(self):
        return f'{self.sender}->{self.recipient}:{self.amount}'

class Block:
    def __init__(self, transactions, previous_hash):
        self.timestamp = time.time()
        self.transactions = transactions  # list of Transaction
        self.previous_hash = previous_hash
        self.nonce = 0
        self.hash = self.calculate_hash()

    def calculate_hash(self):
        tx_str = json.dumps([tx.to_dict() for tx in self.transactions], sort_keys=True)
        payload = f"{self.timestamp}{tx_str}{self.previous_hash}{self.nonce}"
        return hashlib.sha256(payload.encode()).hexdigest()

    def mine_block(self, difficulty):
        target = '0' * difficulty
        while self.hash[:difficulty] != target:
            self.nonce += 1
            self.hash = self.calculate_hash()

# -------------------------
# Blockchain with signature verification
# -------------------------
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
        # reward tx from "system" is allowed without signature
        if tx.sender == "system":
            return True
        if not tx.signature:
            return False
        try:
            vk = VerifyingKey.from_string(bytes.fromhex(tx.sender), curve=SECP256k1)
            message = f"{tx.sender}->{tx.recipient}:{tx.amount}".encode()
            return vk.verify(bytes.fromhex(tx.signature), message)
        except Exception:
            return False

    def create_transaction(self, tx: Transaction):
        if self.verify_transaction(tx):
            self.pending_transactions.append(tx)
            return True
        return False

    def mine_pending_transactions(self, miner_address):
        # Add reward tx into THIS block so miner gets reward immediately
        reward_tx = Transaction("system", miner_address, self.mining_reward, signature=None)
        txs_for_block = self.pending_transactions.copy()
        txs_for_block.append(reward_tx)

        block = Block(txs_for_block, self.get_latest_block().hash)
        block.mine_block(self.difficulty)
        self.chain.append(block)

        # Clear pending transactions after mining
        self.pending_transactions = []
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

# -------------------------
# Flask + SocketIO + P2P
# -------------------------
app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*")
blockchain = Blockchain()

peers = set()  # set of ws://... peers

async def broadcast(message):
    """Send message to all peers (best-effort)."""
    for peer in list(peers):
        try:
            async with websockets.connect(peer) as ws:
                await ws.send(json.dumps(message))
        except Exception:
            # ignore unreachable peers
            pass

async def p2p_server(ws, path):
    async for msg in ws:
        try:
            data = json.loads(msg)
        except Exception:
            continue
        if data.get('type') == 'new_block':
            bd = data.get('data', {})
            # Very simple: append block representation (no deep validation here)
            new_block = Block([], blockchain.get_latest_block().hash)
            new_block.timestamp = bd.get('timestamp', time.time())
            new_block.hash = bd.get('hash', new_block.calculate_hash())
            blockchain.chain.append(new_block)
            socketio.emit('update', {'type': 'block', 'hash': new_block.hash})
        elif data.get('type') == 'new_tx':
            txd = data.get('data', {})
            tx = Transaction(txd.get('sender'), txd.get('recipient'), txd.get('amount'), txd.get('signature'))
            # create_transaction will verify signature
            blockchain.create_transaction(tx)
            socketio.emit('update', {'type': 'transaction'})

async def run_p2p_server(port=6000):
    server = await websockets.serve(p2p_server, "0.0.0.0", port)
    await server.wait_closed()

def start_p2p_in_thread(port=6000):
    threading.Thread(target=lambda: asyncio.run(run_p2p_server(port)), daemon=True).start()

# -------------------------
# Routes
# -------------------------
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/transactions/new', methods=['POST'])
def new_transaction():
    values = request.get_json(force=True)
    tx = Transaction(values.get('sender'), values.get('recipient'), values.get('amount'), values.get('signature'))
    ok = blockchain.create_transaction(tx)
    if ok:
        # broadcast to peers
        try:
            asyncio.run(broadcast({'type': 'new_tx', 'data': values}))
        except Exception:
            pass
        socketio.emit('update', {'type': 'transaction'})
        return 'Transaction added', 201
    else:
        return 'Invalid signature or transaction', 400

# Accept POST (preferred) and GET (backwards compat)
@app.route('/mine', methods=['POST', 'GET'])
def mine():
    if request.method == 'POST':
        data = request.get_json(silent=True) or {}
        miner_address = data.get('address')
    else:
        miner_address = request.args.get('miner')

    if not miner_address:
        return 'Miner address required', 400

    new_block = blockchain.mine_pending_transactions(miner_address)

    # broadcast block (minimal data)
    try:
        asyncio.run(broadcast({'type': 'new_block', 'data': {'timestamp': new_block.timestamp, 'hash': new_block.hash}}))
    except Exception:
        pass

    socketio.emit('update', {'type': 'block', 'hash': new_block.hash})
    return jsonify({'message': 'Block mined', 'block': {'timestamp': new_block.timestamp, 'hash': new_block.hash}}), 200

@app.route('/balance/<address>')
def balance(address):
    return jsonify({'balance': blockchain.get_balance(address)})

@app.route('/chain')
def full_chain():
    chain_data = []
    for block in blockchain.chain:
        chain_data.append({
            'timestamp': block.timestamp,
            'transactions': [t.to_dict() for t in block.transactions],
            'hash': block.hash,
            'prev_hash': block.previous_hash,
            'nonce': block.nonce
        })
    return jsonify({'length': len(chain_data), 'chain': chain_data})

# -------------------------
# Run
# -------------------------
if __name__ == '__main__':
    # start p2p websocket server on port 6000
    start_p2p_in_thread(port=6000)
    # start flask-socketio app
    socketio.run(app, port=5000)
