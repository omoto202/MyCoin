import asyncio, json, time, hashlib, threading, websockets
from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO

class Transaction:
    def __init__(self, sender, recipient, amount, signature=None):
        self.sender = sender
        self.recipient = recipient
        self.amount = amount
        self.signature = signature

    def to_dict(self):
        return {
            "sender": self.sender,
            "recipient": self.recipient,
            "amount": self.amount,
            "signature": self.signature
        }

    def __repr__(self):
        return f"{self.sender}->{self.recipient}:{self.amount}"

class Block:
    def __init__(self, transactions, previous_hash):
        self.timestamp = time.time()
        self.transactions = transactions
        self.previous_hash = previous_hash
        self.nonce = 0
        self.hash = self.calculate_hash()

    def calculate_hash(self):
        txs = [tx.to_dict() for tx in self.transactions]
        payload = json.dumps({
            "timestamp": self.timestamp,
            "transactions": txs,
            "previous_hash": self.previous_hash,
            "nonce": self.nonce
        }, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()

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
        self.mining_reward = 10.0

    def create_genesis_block(self):
        return Block([], "0")

    def get_latest_block(self):
        return self.chain[-1]

    def create_transaction(self, tx: Transaction):
        self.pending_transactions.append(tx)
        return True

    def mine_pending_transactions(self, miner_address):
        reward_tx = Transaction("system", miner_address, f"{self.mining_reward:.8f}")
        txs_to_mine = self.pending_transactions + [reward_tx]
        block = Block(txs_to_mine, self.get_latest_block().hash)
        block.mine_block(self.difficulty)
        self.chain.append(block)
        self.pending_transactions = []
        return block

    def get_balance(self, address):
        if not address:
            return 0.0
        addr = address.lower()
        balance = 0.0
        for block in self.chain:
            for tx in block.transactions:
                try:
                    s = (tx.sender or "").lower()
                    r = (tx.recipient or "").lower()
                    amt = float(tx.amount)
                except:
                    s = tx.sender
                    r = tx.recipient
                    amt = 0.0
                if s == addr:
                    balance -= amt
                if r == addr:
                    balance += amt
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
        except Exception:
            pass

async def p2p_server(ws, path):
    async for raw in ws:
        try:
            data = json.loads(raw)
            t = data.get("type")
            if t == "new_tx":
                txd = data.get("data", {})
                tx = Transaction(txd.get("sender"), txd.get("recipient"), txd.get("amount"), txd.get("signature"))
                blockchain.create_transaction(tx)
                socketio.emit('update', {'type': 'transaction'})
            elif t == "new_block":
                socketio.emit('update', {'type': 'block', 'data': data.get("data")})
        except Exception as e:
            print("p2p_server error:", e)

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
    if not values:
        return 'Missing body', 400
    required = ['sender', 'recipient', 'amount']
    if not all(k in values for k in required):
        return 'Missing values', 400

    tx = Transaction(values['sender'], values['recipient'], values['amount'], values.get('signature'))
    blockchain.create_transaction(tx)

    try:
        asyncio.run(broadcast({'type': 'new_tx', 'data': tx.to_dict()}))
    except:
        pass

    socketio.emit('update', {'type': 'transaction'})
    return 'Transaction accepted', 201

@app.route('/mine_with_chain', methods=['POST'])
def mine_with_chain():
    data = request.get_json()
    chain = data.get("chain")

    if chain is None or len(chain) == 0:
        return jsonify({"error": "chain required"}), 400

    last_block = chain[-1]
    last_hash = last_block["hash"]

    # 新しいブロックを生成（transactions は任意で適用）
    new_block = {
        "index": last_block["index"] + 1,
        "timestamp": time.time(),
        "transactions": pending_transactions.copy(),
        "previous_hash": last_hash,
        "nonce": 0
    }

    # PoW（簡略版）
    while True:
        block_string = json.dumps(new_block, sort_keys=True).encode()
        block_hash = hashlib.sha256(block_string).hexdigest()
        if block_hash.startswith("0000"):
            break
        new_block["nonce"] += 1

    new_block["hash"] = block_hash

    # pending をリセット
    pending_transactions.clear()

    return jsonify(new_block)

@app.route('/balance/<address>')
def balance(address):
    try:
        amt = float(blockchain.get_balance(address))
    except:
        amt = 0.0
    return jsonify({'balance': amt})

@app.route('/chain')
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
    start_p2p_in_thread(port=6000)
    socketio.run(app, host='0.0.0.0', port=5000)
