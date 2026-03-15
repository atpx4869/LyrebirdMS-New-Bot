import pymysql

from app_config import config
from logger.logger import write_log

host = config['host']
port = config['port']
user = config['user']
password = config['password']
database = config['database']


def _get_conn():
    return pymysql.connect(host=host, port=port, user=user, passwd=password, db=database, charset='utf8mb4', autocommit=False)


def read_user_info(userid):
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute('SELECT iv, lv, embyid, name, free FROM emby WHERE tg=%s', (userid,))
            return cur.fetchone()
    except Exception as e:
        write_log(f'查询用户信息失败 tg={userid}: {e}', level='ERROR')
        return None
    finally:
        conn.close()


def update_user_info(userid, coins):
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute('UPDATE emby SET iv = iv + %s WHERE tg=%s', (coins, userid))
            affected_rows = cur.rowcount
        conn.commit()
        return 1 if affected_rows > 0 else 0
    except Exception as e:
        conn.rollback()
        write_log(f'更新用户积分失败 tg={userid}: {e}', level='ERROR')
        return 0
    finally:
        conn.close()


def update_user_info_free(userid, coins, free):
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            if free + coins >= 0:
                cur.execute('UPDATE emby SET free = free + %s WHERE tg=%s', (coins, userid))
            else:
                cur.execute('UPDATE emby SET free = 0 WHERE tg=%s', (userid,))
                cur.execute('UPDATE emby SET iv = iv + %s WHERE tg=%s', (free + coins, userid))
            affected_rows = cur.rowcount
        conn.commit()
        return 1 if affected_rows > 0 else 0
    except Exception as e:
        conn.rollback()
        write_log(f'更新用户免费额度失败 tg={userid}: {e}', level='ERROR')
        return 0
    finally:
        conn.close()


def list_recent_users(limit=50):
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute('SELECT tg, name, iv, free, lv FROM emby ORDER BY tg DESC LIMIT %s', (limit,))
            rows = cur.fetchall()
            return [
                {'tg': row[0], 'name': row[1], 'iv': row[2], 'free': row[3], 'lv': row[4]}
                for row in rows
            ]
    except Exception as e:
        write_log(f'查询用户列表失败: {e}', level='ERROR')
        return []
    finally:
        conn.close()


def get_user_stats():
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute('SELECT COUNT(*) FROM emby')
            total = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM emby WHERE lv <> 'd'")
            active = cur.fetchone()[0]
            return {'total_users': total, 'active_users': active}
    except Exception as e:
        write_log(f'查询用户统计失败: {e}', level='ERROR')
        return {'total_users': 0, 'active_users': 0}
    finally:
        conn.close()


def search_users(query='', limit=100):
    conn = _get_conn()
    try:
        like = f'%{query}%'
        with conn.cursor() as cur:
            cur.execute('SELECT tg, name, iv, free, lv FROM emby WHERE (%s = '' OR CAST(tg AS CHAR) LIKE %s OR name LIKE %s) ORDER BY tg DESC LIMIT %s', (query, like, like, limit))
            rows = cur.fetchall()
            return [{'tg': row[0], 'name': row[1], 'iv': row[2], 'free': row[3], 'lv': row[4]} for row in rows]
    except Exception as e:
        write_log(f'搜索用户失败 query={query}: {e}', level='ERROR')
        return []
    finally:
        conn.close()


def get_user_detail(userid):
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute('SELECT tg, name, iv, free, lv, embyid FROM emby WHERE tg=%s', (userid,))
            row = cur.fetchone()
            if not row:
                return None
            return {'tg': row[0], 'name': row[1], 'iv': row[2], 'free': row[3], 'lv': row[4], 'embyid': row[5]}
    except Exception as e:
        write_log(f'查询用户详情失败 tg={userid}: {e}', level='ERROR')
        return None
    finally:
        conn.close()



def admin_adjust_user(userid, coins_delta=0, free_delta=0, level=None):
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            affected = 0
            if coins_delta:
                cur.execute('UPDATE emby SET iv = iv + %s WHERE tg=%s', (coins_delta, userid))
                affected = max(affected, cur.rowcount)
            if free_delta:
                cur.execute('UPDATE emby SET free = GREATEST(free + %s, 0) WHERE tg=%s', (free_delta, userid))
                affected = max(affected, cur.rowcount)
            if level is not None and str(level).strip() != '':
                cur.execute('UPDATE emby SET lv = %s WHERE tg=%s', (str(level).strip(), userid))
                affected = max(affected, cur.rowcount)
        conn.commit()
        return affected > 0
    except Exception as e:
        conn.rollback()
        write_log(f'管理员调整用户失败 tg={userid}: {e}', level='ERROR')
        return False
    finally:
        conn.close()


def get_user_download_summary(userid):
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute('SELECT COUNT(*) FROM emby WHERE tg=%s', (userid,))
            exists = cur.fetchone()[0] > 0
            return {'exists': exists}
    except Exception as e:
        write_log(f'查询用户摘要失败 tg={userid}: {e}', level='ERROR')
        return {'exists': False}
    finally:
        conn.close()
