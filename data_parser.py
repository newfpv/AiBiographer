from datetime import datetime

def get_all_messages(data):
    """Универсальная функция: достает сообщения и из полного дампа, и из соло-чата"""
    all_messages = []
    
    # Сценарий 1: Это ПОЛНЫЙ экспорт всего аккаунта
    if 'chats' in data and isinstance(data['chats'], dict) and 'list' in data['chats']:
        for chat in data['chats']['list']:
            if 'messages' in chat:
                chat_name = chat.get('name', 'Неизвестный чат')
                for msg in chat['messages']:
                    # Прикрепляем к сообщению метку, из какого оно чата
                    msg['_chat_source'] = chat_name
                    all_messages.append(msg)
                    
    # Сценарий 2: Это экспорт только одного конкретного чата
    elif 'messages' in data:
        all_messages.extend(data['messages'])
        
    return all_messages

def parse_messages(data, name_map, progress_cb=None):
    days_data = {}
    messages = get_all_messages(data)
    total_msgs = len(messages)
    
    for idx, msg in enumerate(messages):
        if progress_cb and idx % 15000 == 0:
            progress_cb(idx, total_msgs)
            
        if msg.get('type') == 'message' and msg.get('date'):
            try: dt = datetime.fromisoformat(msg['date'])
            except: continue
            date_str = dt.strftime('%Y.%m.%d')
            sender = name_map.get(msg.get('from', 'Неизвестно'), msg.get('from', 'Неизвестно'))
            
            ctx = []
            # Добавляем контекст чата, если это глобальный дамп
            if '_chat_source' in msg:
                ctx.append(f"Чат: {msg['_chat_source']}")
                
            if 'forwarded_from' in msg:
                fwd = name_map.get(msg['forwarded_from'], msg['forwarded_from'])
                ctx.append("Переслал свое" if fwd == sender else f"Переслано от {fwd}")
            if 'reply_to_message_id' in msg: ctx.append("В ответ")
            t_str = f" [{', '.join(ctx)}]" if ctx else ""
            
            text = ''.join(i if isinstance(i, str) else i.get('text', '') for i in msg.get('text', '')) if isinstance(msg.get('text'), list) else str(msg.get('text', ''))
            text = text.strip().replace('\n', ' ')
            
            m_tag = ""
            if 'photo' in msg: m_tag = "[Фото]"
            elif msg.get('media_type') == 'video_file': m_tag = "[Видео]"
            elif msg.get('media_type') == 'voice_message': m_tag = "[Голосовое]"
            elif msg.get('media_type') == 'video_message': m_tag = "[Кружок]"
            elif msg.get('media_type') == 'animation': m_tag = "[GIF]"
            elif msg.get('media_type') == 'sticker': m_tag = "[Стикер]"
            elif 'file' in msg and 'media_type' not in msg: m_tag = "[Файл]"
            
            fin = f"{m_tag} {text}".strip()
            if fin:
                if date_str not in days_data: days_data[date_str] = []
                days_data[date_str].append((dt, f"{date_str} {sender}{t_str}: {fin}"))
    return days_data

def chunk_data(days_data, limit):
    total_msgs = sum(len(v) for v in days_data.values())
    chunks = []
    if total_msgs < 20000 and limit == 2000:
        cl, sd = [], None
        for d in sorted(days_data.keys()):
            if not sd: sd = d
            cl.extend([l for _, l in days_data[d]])
            if len(cl) >= limit:
                chunks.append({'title': f"{sd} - {d}", 'lines': cl})
                cl, sd = [], None
        if cl: chunks.append({'title': f"{sd} - {d}", 'lines': cl})
    else:
        y_dict = {}
        for d in sorted(days_data.keys()):
            dt = days_data[d][0][0]
            y, q, m = dt.year, (dt.month - 1) // 3 + 1, dt.month
            if y not in y_dict: y_dict[y] = {}
            if q not in y_dict[y]: y_dict[y][q] = {}
            if m not in y_dict[y][q]: y_dict[y][q][m] = {}
            y_dict[y][q][m][d] = [l for _, l in days_data[d]]
        for y, qts in sorted(y_dict.items()):
            if sum(len(l) for q in qts.values() for m in q.values() for l in m.values()) <= limit:
                chunks.append({'title': f"Год {y}", 'lines': [x for q in qts.values() for m in q.values() for l in m.values() for x in l]})
                continue
            for q, mth in sorted(qts.items()):
                if sum(len(l) for m in mth.values() for l in m.values()) <= limit:
                    chunks.append({'title': f"{y} Кв {q}", 'lines': [x for m in mth.values() for l in m.values() for x in l]})
                    continue
                for m, m_d in sorted(mth.items()):
                    if sum(len(l) for l in m_d.values()) <= limit:
                        chunks.append({'title': f"{y}-{m:02d}", 'lines': [x for l in m_d.values() for x in l]})
                        continue
                    cl, sd, p = [], None, 1
                    for d in sorted(m_d.keys()):
                        if not sd: sd = d
                        cl.extend(m_d[d])
                        if len(cl) >= limit:
                            chunks.append({'title': f"{y}-{m:02d} (Ч {p})", 'lines': cl})
                            cl, sd, p = [], None, p + 1
                    if cl: chunks.append({'title': f"{y}-{m:02d} (Ч {p})", 'lines': cl})
    return chunks