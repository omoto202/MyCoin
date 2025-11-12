import asyncio, json, time, hashlib, threading
from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO
import websockets
from ecdsa import VerifyingKey, SECP256k1, BadSignatureError

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
        return {
            "sender": self.sender,
            "recipient": self.recipient,
            "amount": self.amount,
            "signature": self.signature
        }

    def __repr__(self):
        return f'{self.sender}->{self.recipient}:{self.amount}'

class Block:
    def __init__(self, transactions, previous_hash):
        self.timestamp = time.time()
        # transactions are list of Transaction
        self.transactions = transactions
        self.previous_hash = previous_hash
        self.nonce = 0
        self.hash = self.calculate_hash()

    def calculate_hash(self):
        txs = [tx.to_dict() for tx in self.transactions]
        data = json.dumps({
            "timestamp": self.timestamp,
            "transactions": txs,
            "previous_hash": self.previous_hash,
            "nonce": self.nonce
        }, sort_keys=True)
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

    # 验証：署名のチェック（systemは常に有効）
    def verify_transaction(self, tx: Transaction):
        if tx.sender == "system":
            return True
        if not tx.signature or not tx.sender:
            return False
        try:
            pub_hex = tx.sender
            # elliptic の公開鍵は先頭に '04' がつく非圧縮形式が来ることが多いので対応
            if pub_hex.startswith("04") or pub_hex.startswith("0x04"):
                pub_hex_stripped = pub_hex[2:] if not pub_hex.startswith("0x") else pub_hex[4:]
            else:
                pub_hex_stripped = pub_hex
            pub_bytes = bytes.fromhex(pub_hex_stripped)
            vk = VerifyingKey.from_string(pub_bytes, curve=SECP256k1)
            # サイン検証に使うメッセージはクライアントと全く同じ文字列にする
            message = f"{tx.sender}->{tx.recipient}:{tx.amount}".encode()
            sig_bytes = bytes.fromhex(tx.signature)
            return vk.verify(sig_bytes, message)
        except BadSignatureError:
            return False
        except Exception as e:
            print("Signature verify error:", e)
            return False

    # create_transaction は検証を行う
    def create_transaction(self, tx: Transaction):
        if self.verify_transaction(tx):
            self.pending_transactions.append(tx)
            return True
        return False

    # 報酬を同じブロックに入れて即時反映させる実装
    def mine_pending_transactions(self, miner_address):
        # 報酬Txを pending に追加してからそのままブロック化（＝1回で報酬反映）
        reward_tx = Transaction("system", miner_address, self.mining_reward)
        self.pending_transactions.append(reward_tx)

        block = Block(self.pending_transactions, self.get_latest_block().hash)
        block.mine_block(self.difficulty)
        self.chain.append(block)

        # pending をクリア（既に入れた報酬もブロック内）
        self.pending_transactions = []
        return block

    def get_balance(self, address):
        balance = 0
        if not address:
            return 0
        addr = address.lower()
        for block in self.chain:
            for tx in block.transactions:
                try:
                    sender = (tx.sender or "").lower()
                    recipient = (tx.recipient or "").lower()
                except:
                    sender = tx.sender
                    recipient = tx.recipient
                if sender == addr:
                    balance -= tx.amount
                if recipient == addr:
                    balance += tx.amount
        return balance

# -------------------------
# Flask + Socket.IO + P2P
# -------------------------
app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*")
blockchain = Blockchain()

peers = set()  # ws://host:port の文字列を追加して使う

async def broadcast(message):
    """非同期で全ピアに JSON メッセージを送信"""
    for peer in list(peers):
        try:
            async with websockets.connect(peer) as ws:
                await ws.send(json.dumps(message))
        except Exception as e:
            # 接続できないピアはスキップ（必要なら peers から削除する処理を追加）
            # print("broadcast error:", e)
            pass

async def p2p_server(ws, path):
    async for msg in ws:
        try:
            data = json.loads(msg)
            t = data.get("type")
            if t == "new_block":
                b = data.get("data", {})
                # ここではシンプルに外部ブロックのハッシュだけチェーンに追加しない。
                # 実運用ではブロック検証・チェーン置換処理を入れるべきです。
                print("Received new_block from peer:", b.get("hash"))
            elif t == "new_tx":
                txd = data.get("data", {})
                tx = Transaction(txd.get("sender"), txd.get("recipient"), txd.get("amount"), txd.get("signature"))
                # create_transaction で署名検証を行う
                success = blockchain.create_transaction(tx)
                if success:
                    socketio.emit('update', {'type':'transaction'})
        except Exception as e:
            print("p2p message handling error:", e)

async def run_p2p_server(port=6000):
    server = await websockets.serve(p2p_server, "0.0.0.0", port)
    await server.wait_closed()

def start_p2p_in_thread(port=6000):
    threading.Thread(target=lambda: asyncio.run(run_p2p_server(port)), daemon=True).start()

# -------------------------
# Routes / API
# -------------------------
@app.route('/')
def index():
    return render_template('index.html')

# 新しいトランザクション受け取り（署名検証あり）
@app.route('/transactions/new', methods=['POST'])
def new_transaction():
    values = request.get_json()
    required = ['sender','recipient','amount','signature']
    if not values or not all(k in values for k in required):
        return 'Missing values', 400

    tx = Transaction(values['sender'], values['recipient'], values['amount'], values['signature'])
    if not blockchain.create_transaction(tx):
        return 'Invalid signature or transaction', 400

    # P2Pで転送（非同期）
    try:
        asyncio.run(broadcast({'type':'new_tx','data':tx.to_dict()}))
    except:
        pass

    # Socket.IOでブラウザ通知（即時）
    socketio.emit('update', {'type':'transaction'})
    return 'Transaction accepted', 201

# マイニング（POSTで miner アドレスを受け取り、報酬を同ブロックに含めて即反映）
@app.route('/mine', methods=['POST'])
def mine():
    values = request.get_json() or {}
    miner_address = values.get('miner')
    if not miner_address:
        return 'Miner address required', 400

    new_block = blockchain.mine_pending_transactions(miner_address)

    # P2P broadcast
    try:
        asyncio.run(broadcast({'type':'new_block','data':{
            'timestamp': new_block.timestamp,
            'hash': new_block.hash
        }}))
    except:
        pass

    # Socket.IO でクライアントに通知
    socketio.emit('update', {'type':'block','hash':new_block.hash, 'miner': miner_address})

    return jsonify({
        'message': 'Block mined successfully',
        'miner': miner_address,
        'block_hash': new_block.hash
    }), 200

@app.route('/balance/<address>')
def balance(address):
    amount = blockchain.get_balance(address)
    return jsonify({'balance': amount})

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
# Run server
# -------------------------
if __name__ == '__main__':
    # P2P サーバー（デフォルト port=6000）を別スレッドで起動
    start_p2p_in_thread(port=6000)
    # Socket.IO で Flask 実行（開発時はこのままでOK。productionでは gunicorn+eventlet 推奨）
    socketio.run(app, host='0.0.0.0', port=5000)
