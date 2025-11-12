# app.py
import asyncio
import json
import time
import hashlib
import threading
from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO
import websockets
from ecdsa import VerifyingKey, SECP256k1, BadSignatureError

# -------------------------
# Data classes
# -------------------------
class Transaction:
    def __init__(self, sender, recipient, amount, signature=None):
        self.sender = sender
        self.recipient = recipient
        # amount may come as string (fixed format). Keep as-is but treat numerically when needed.
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
        self.transactions = transactions  # list of Transaction
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

# -------------------------
# Blockchain
# -------------------------
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

    # verify signature: message format must match client exactly
    def verify_transaction(self, tx: Transaction):
        if tx.sender == "system":
            return True
        if not tx.signature or not tx.sender:
            return False
        try:
            pub_hex = tx.sender
            # If public key comes with '04' prefix (uncompressed), strip it for VerifyingKey
            if pub_hex.startswith("04"):
                pub_hex_for_vk = pub_hex[2:]
            else:
                pub_hex_for_vk = pub_hex
            pub_bytes = bytes.fromhex(pub_hex_for_vk)
            vk = VerifyingKey.from_string(pub_bytes, curve=SECP256k1)

            # message must be formatted exactly as client signs:
            # sender->recipient:amount  with amount formatted to 8 decimals
            msg_text = f"{tx.sender}->{tx.recipient}:{float(tx.amount):.8f}"
            msg = msg_text.encode()
            sig_bytes = bytes.fromhex(tx.signature)
            # verify will raise BadSignatureError if invalid
            vk.verify(sig_bytes, msg)
            return True
        except BadSignatureError:
            # signature invalid
            return False
        except Exception as e:
            # any other error, treat as invalid
            print("verify_transaction error:", e)
            return False

    # create transaction after verifying signature and balance
    def create_transaction(self, tx: Transaction):
        if not self.verify_transaction(tx):
            return False
        # balance check for non-system senders
        if tx.sender != "system":
            try:
                amt = float(tx.amount)
            except:
                return False
            if self.get_balance(tx.sender) < amt:
                return False
        self.pending_transactions.append(tx)
        return True

    # include reward in same block (immediate reflection)
    def mine_pending_transactions(self, miner_address):
        # create reward tx and include it in this block
        reward_tx = Transaction("system", miner_address, f"{self.mining_reward:.8f}", None)
        # ensure reward appended
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
                except:
                    s = tx.sender
                    r = tx.recipient
                try:
                    amt = float(tx.amount)
                except:
                    amt = 0.0
                if s == addr:
                    balance -= amt
                if r == addr:
                    balance += amt
        return balance

# -------------------------
# Flask + SocketIO + P2P (simple)
# -------------------------
app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*")
blockchain = Blockchain()

# simple peers set for P2P (ws://host:port)
peers = set()

async def broadcast(message):
    for peer in list(peers):
        try:
            async with websockets.connect(peer) as ws:
                await ws.send(json.dumps(message))
        except Exception:
            # ignore peers that can't be reached
            pass

async def p2p_server(ws, path):
    async for raw in ws:
        try:
            data = json.loads(raw)
            t = data.get("type")
            if t == "new_tx":
                txd = data.get("data", {})
                tx = Transaction(txd.get("sender"), txd.get("recipient"), txd.get("amount"), txd.get("signature"))
                if blockchain.create_transaction(tx):
                    socketio.emit('update', {'type': 'transaction'})
            elif t == "new_block":
                # in this simple demo we do not perform full chain replace
                # real implementation should validate block and maybe replace chain
                socketio.emit('update', {'type': 'block', 'data': data.get("data")})
        except Exception as e:
            print("p2p_server error:", e)

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
    values = request.get_json()
    if not values:
        return 'Missing body', 400
    required = ['sender', 'recipient', 'amount', 'signature']
    if not all(k in values for k in required):
        return 'Missing values', 400

    # ensure amount string consistent (server expects fixed format)
    try:
        amt = float(values['amount'])
        values['amount'] = f"{amt:.8f}"
    except:
        return 'Invalid amount', 400

    tx = Transaction(values['sender'], values['recipient'], values['amount'], values['signature'])
    ok = blockchain.create_transaction(tx)
    if not ok:
        return 'Invalid signature or insufficient balance', 400

    # broadcast to peers asynchronously
    try:
        asyncio.run(broadcast({'type': 'new_tx', 'data': tx.to_dict()}))
    except Exception:
        pass

    # notify connected clients via Socket.IO
    socketio.emit('update', {'type': 'transaction'})
    return 'Transaction accepted', 201

@app.route('/mine', methods=['POST'])
def mine():
    values = request.get_json() or {}
    miner = values.get('miner')
    if not miner:
        return 'Miner address required', 400

    new_block = blockchain.mine_pending_transactions(miner)

    # broadcast block to peers
    try:
        asyncio.run(broadcast({'type': 'new_block', 'data': {
            'timestamp': new_block.timestamp,
            'hash': new_block.hash
        }}))
    except:
        pass

    # notify clients
    socketio.emit('update', {'type': 'block', 'hash': new_block.hash, 'miner': miner})

    return jsonify({
        'message': 'Block mined successfully',
        'miner': miner,
        'block_hash': new_block.hash
    }), 200

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

# -------------------------
# Run
# -------------------------
if __name__ == '__main__':
    start_p2p_in_thread(port=6000)
    # Use socketio.run to serve with Socket.IO support
    socketio.run(app, host='0.0.0.0', port=5000)
