from flask import Flask, request, jsonify
import database as db
import logging

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

# Инициализация БД при запуске сервера
db.init_db()

@app.route('/calls', methods=['GET'])
def get_all_calls():
    # Можно передавать ?limit=N
    limit = request.args.get('limit', type=int)
    if limit:
        return jsonify(db.get_recent_calls(limit))
    return jsonify(db.get_all_calls())

@app.route('/calls/<int:call_id>', methods=['GET'])
def get_call(call_id):
    call = db.get_call_by_id(call_id)
    if call:
        return jsonify(call)
    return jsonify({"error": "Not found"}), 404

@app.route('/calls/search', methods=['GET'])
def search():
    query = request.args.get('q', '')
    limit = request.args.get('limit', default=50, type=int)
    results = db.search_calls(query, limit)
    return jsonify(results)

@app.route('/calls', methods=['POST'])
def add_call():
    data = request.json
    try:
        call_id = db.add_call(
            phone=data['phone'],
            name=data['name'],
            call_date=data['call_date'],
            remind_date=data.get('remind_date'),
            notes=data.get('notes')
        )
        return jsonify({"id": call_id, "success": True}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route('/calls/<int:call_id>', methods=['PUT'])
def update_call(call_id):
    data = request.json
    try:
        # update_call возвращает True если обновил
        updated = db.update_call(call_id, **data)
        return jsonify({"success": updated, "id": call_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route('/calls/<int:call_id>', methods=['DELETE'])
def delete_call(call_id):
    success = db.delete_call(call_id)
    return jsonify({"success": success})


if __name__ == '__main__':
    # Слушать на всех интерфейсах (0.0.0.0) порт 5000
    app.run(host='0.0.0.0', port=5000)
