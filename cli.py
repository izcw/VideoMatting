"""VideoMatting 远程客户端：仅使用 Python 标准库，无需模型或 GPU 依赖。"""
import argparse
import http.client
import json
import os
from pathlib import Path
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


class Client:
    def __init__(self, server, token, timeout=180):
        parsed = urllib.parse.urlsplit(server.rstrip('/'))
        if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.path not in {'', '/'} or parsed.query or parsed.fragment or parsed.username:
            raise ValueError('服务地址请使用 http://显卡电脑IP:8765，不带路径或账号。')
        self.server = server.rstrip('/')
        self.parsed = parsed
        self.token = token
        self.timeout = timeout
        # 局域网调用不经过工作机的系统代理。
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(self, path, method='GET', data=None):
        if not path.startswith('/api/v1/'):
            raise ValueError('服务返回了无效的集成 API 路径。')
        headers = {'Authorization': 'Bearer ' + self.token}
        body = None
        if data is not None:
            body = json.dumps(data).encode('utf-8')
            headers['Content-Type'] = 'application/json'
        request = urllib.request.Request(self.server + path, data=body, headers=headers, method=method)
        return self.opener.open(request, timeout=self.timeout)

    def json(self, path, method='GET', data=None):
        with self.request(path, method, data) as response:
            return json.load(response)

    def upload(self, path):
        path = Path(path)
        if path.suffix.lower() not in {'.mp4', '.mov', '.webm', '.mkv'}:
            raise ValueError('支持 MP4、MOV、WebM、MKV。')
        if not 0 < path.stat().st_size <= 250 * 1024 * 1024:
            raise ValueError('视频必须非空且不超过 250 MB。')
        boundary = 'VideoMatting' + secrets.token_hex(12)
        filename = path.name.replace('"', '_').replace('\r', '_').replace('\n', '_')
        start = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
                 'Content-Type: application/octet-stream\r\n\r\n').encode('utf-8')
        end = f'\r\n--{boundary}--\r\n'.encode()
        cls = http.client.HTTPSConnection if self.parsed.scheme == 'https' else http.client.HTTPConnection
        connection = cls(self.parsed.hostname, self.parsed.port, timeout=self.timeout)
        try:
            with path.open('rb') as stream:
                size = os.fstat(stream.fileno()).st_size
                connection.putrequest('POST', '/api/v1/jobs')
                connection.putheader('Authorization', 'Bearer ' + self.token)
                connection.putheader('Content-Type', 'multipart/form-data; boundary=' + boundary)
                connection.putheader('Content-Length', str(len(start) + size + len(end)))
                connection.endheaders()
                connection.send(start)
                while chunk := stream.read(1024 * 1024):
                    connection.send(chunk)
                connection.send(end)
            response = connection.getresponse()
            payload = response.read()
            if response.status >= 400:
                raise RuntimeError(f'HTTP {response.status}: ' + payload.decode('utf-8', 'replace')[:4096])
            return json.loads(payload)
        finally:
            connection.close()

    def wait(self, job_id, timeout=0, interval=2):
        started = time.monotonic()
        previous = None
        while True:
            job = self.json('/api/v1/jobs/' + job_id)
            state = (job['status'], job.get('progress'), job.get('stage'))
            if state != previous:
                print(json.dumps({'id': job_id, 'status': state[0], 'progress': state[1], 'stage': state[2]}, ensure_ascii=False), file=sys.stderr)
                previous = state
            if job['status'] in {'done', 'failed', 'cancelled', 'interrupted'}:
                return job
            if timeout and time.monotonic() - started >= timeout:
                raise TimeoutError('等待超时，远端任务仍继续运行。可用 status 或 wait 再次查询。')
            time.sleep(interval)

    def download(self, job_id, mode='transparent', color='#3b82f6', audio=False, output=None, overwrite=False):
        links = self.json(f'/api/v1/jobs/{job_id}/export', 'POST', dict(mode=mode, color=color, audio=audio))
        kind = {'transparent': 'WebM', 'custom': 'Background', 'matte': 'AlphaMatte'}[mode]
        suffix = '.webm' if mode == 'transparent' else '.mp4'
        target = Path(output or f'VideoMatting-{kind}-{job_id.replace("-", "")[:12]}{suffix}')
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and not overwrite:
            raise FileExistsError('目标文件已存在；请指定其他 --output，或使用 --overwrite。')
        temporary = target.with_name(target.name + '.' + secrets.token_hex(4) + '.part')
        try:
            with self.request(links['download']) as response, temporary.open('xb') as stream:
                while chunk := response.read(1024 * 1024):
                    stream.write(chunk)
            if overwrite:
                os.replace(temporary, target)
            else:
                # 创建时再次检查存在性，避免并发下载覆盖另一份文件。
                with target.open('xb') as stream, temporary.open('rb') as source:
                    while chunk := source.read(1024 * 1024):
                        stream.write(chunk)
        finally:
            temporary.unlink(missing_ok=True)
        return {'file': str(target.resolve()), 'bytes': target.stat().st_size}


def parser():
    result = argparse.ArgumentParser(description='VideoMatting 远程任务与诊断客户端')
    result.add_argument('--server', default=os.environ.get('VIDEOMATTING_SERVER', 'http://127.0.0.1:8765'))
    result.add_argument('--token-file', help='显卡电脑 data/api-token.txt 的本地副本；也可设置 VIDEOMATTING_API_TOKEN')
    commands = result.add_subparsers(dest='command', required=True)
    for name in ('health', 'system', 'diagnostics', 'diagnose', 'diagnose-status'):
        commands.add_parser(name)
    listing = commands.add_parser('list')
    listing.add_argument('--all', action='store_true', help='包括被网页历史列表隐藏的任务')
    upload = commands.add_parser('upload')
    upload.add_argument('video')
    upload.add_argument('--wait', action='store_true')
    upload.add_argument('--output', help='等待成功后下载透明 WebM 至指定位置')
    upload.add_argument('--timeout', type=float, default=0, help='等待秒数，0 为不限时')
    for name in ('status', 'cancel', 'resume', 'delete', 'wait', 'download'):
        command = commands.add_parser(name)
        command.add_argument('job_id')
        if name == 'delete':
            command.add_argument('--yes', action='store_true', help='确认永久删除远端任务及处理文件')
        if name == 'wait':
            command.add_argument('--timeout', type=float, default=0)
        if name == 'download':
            command.add_argument('--mode', choices=['transparent', 'custom', 'matte'], default='transparent')
            command.add_argument('--color', default='#3b82f6')
            command.add_argument('--audio', action='store_true')
            command.add_argument('--output')
            command.add_argument('--overwrite', action='store_true')
    logs = commands.add_parser('logs')
    logs.add_argument('--name', choices=['server', 'server-error', 'system-check'], default='server-error')
    logs.add_argument('--job')
    logs.add_argument('--kind', choices=['events', 'error'], default='events')
    logs.add_argument('--lines', type=int, default=200)
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        token = Path(args.token_file).read_text(encoding='utf-8').strip() if args.token_file else os.environ.get('VIDEOMATTING_API_TOKEN', '').strip()
        if not token:
            raise ValueError('请用 --token-file 提供访问密钥文件，或设置 VIDEOMATTING_API_TOKEN。')
        client = Client(args.server, token)
        command = args.command
        if hasattr(args, 'job_id'):
            args.job_id = str(uuid.UUID(args.job_id))
        if command in {'health', 'system', 'diagnostics'}:
            value = client.json('/api/v1/' + command)
        elif command in {'diagnose', 'diagnose-status'}:
            value = client.json('/api/v1/diagnostics/system-check', 'POST' if command == 'diagnose' else 'GET')
        elif command == 'list':
            value = client.json('/api/v1/jobs?include_hidden=' + str(args.all).lower())
        elif command == 'upload':
            if args.output and not args.wait:
                raise ValueError('--output 需要同时指定 --wait。')
            value = client.upload(args.video)
            if args.wait:
                value = client.wait(value['id'], args.timeout)
                if value['status'] == 'done' and args.output:
                    value['download'] = client.download(value['id'], output=args.output)
        elif command == 'wait':
            value = client.wait(args.job_id, args.timeout)
        elif command == 'download':
            value = client.download(args.job_id, args.mode, args.color, args.audio, args.output, args.overwrite)
        elif command == 'logs':
            query = '?lines=' + str(args.lines)
            if args.job:
                value = client.json('/api/v1/jobs/' + str(uuid.UUID(args.job)) + '/logs' + query + '&kind=' + args.kind)
            else:
                value = client.json('/api/v1/logs/' + args.name + query)
        elif command == 'delete':
            if not args.yes:
                raise ValueError('删除会同时永久移除远端原片副本、结果与检查点，请加 --yes 确认。')
            value = client.json('/api/v1/jobs/' + args.job_id, 'DELETE')
        elif command in {'cancel', 'resume'}:
            value = client.json(f'/api/v1/jobs/{args.job_id}/{command}', 'POST')
        else:
            value = client.json('/api/v1/jobs/' + args.job_id)
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return 2 if command in {'wait', 'upload'} and value.get('status') in {'failed', 'cancelled', 'interrupted'} else 0
    except urllib.error.HTTPError as error:
        print(f'HTTP {error.code}: ' + error.read(4096).decode('utf-8', 'replace'), file=sys.stderr)
    except (OSError, ValueError, RuntimeError, http.client.HTTPException) as error:
        print('调用失败：' + str(error), file=sys.stderr)
    except KeyboardInterrupt:
        print('已停止等待；远端任务仍继续运行。取消任务请使用 cancel。', file=sys.stderr)
        return 130
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
