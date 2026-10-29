"""Publish a verified, already signed APK; never builds or handles a signing key."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile


def gh(*args, binary=False):
    return subprocess.check_output(['gh', *args], text=not binary)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    config = json.loads(Path('publication.json').read_text())
    repo = os.environ['GH_REPO']
    version, code = config['versionName'], config['versionCode']
    expected = config['sha256']
    assert re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', version)
    assert type(code) is int and code > 0
    assert re.fullmatch(r'[0-9a-f]{64}', expected)
    assert config['packageName'] == 'dev.mark.utvremote'
    assert config['certificateSha256'] == 'b7b518662cb14be645c7c1f805d7a2c1e8b58dc4fa282912beaf7e97dcd74180'
    assert 0 < len(config['parts']) <= 20
    tag, filename = 'v' + version, 'utv-' + version + '.apk'
    manifest = {
        'versionCode': code, 'versionName': version,
        'apkUrl': f'https://github.com/{repo}/releases/download/{tag}/{filename}',
        'sha256': expected, 'notes': config['notes'][:3000],
    }
    # Get the current feed before publication; its SHA makes the final write conditional.
    feed_path = f'repos/{repo}/contents/release.json'
    entries = json.loads(gh('api', f'repos/{repo}/contents/'))
    current = json.loads(gh('api', feed_path)) if any(entry['name'] == 'release.json' for entry in entries) else None
    if current:
        import base64
        previous = json.loads(base64.b64decode(current['content']))
        assert previous['versionCode'] <= code, 'Refusing to downgrade the update feed'
        if previous['versionCode'] == code:
            assert previous == manifest, 'Same version has different metadata'
    work = Path(tempfile.mkdtemp(prefix='utv-release-'))
    apk = work / filename
    with apk.open('wb') as out:
        for sha in config['parts']:
            assert re.fullmatch(r'[0-9a-f]{40}', sha)
            data = gh('api', f'repos/{repo}/git/blobs/{sha}', '-H', 'Accept: application/vnd.github.raw+json', binary=True)
            out.write(data)
            assert out.tell() <= 50 * 1024 * 1024
    assert digest(apk) == expected, 'APK checksum mismatch'
    sdk = Path(os.environ['ANDROID_HOME']) / 'build-tools'
    tools = sdk / '36.0.0'
    cert = subprocess.check_output([str(tools/'apksigner'),'verify','--print-certs',str(apk)],text=True)
    print(cert, flush=True)
    matches = re.findall(r'Signer #\d+ certificate SHA-256 digest: ([0-9a-fA-F]+)',cert)
    assert len(matches) == 1 and matches[0].lower() == config['certificateSha256'], 'Wrong signing certificate'
    badging = subprocess.check_output([str(tools/'aapt'),'dump','badging',str(apk)],text=True)
    match = re.search(r"package: name='([^']+)' versionCode='(\d+)' versionName='([^']+)'",badging)
    assert match and match.groups() == (config['packageName'],str(code),version), 'Wrong package/version'
    (work/'SHA256SUMS').write_text(expected+'  '+filename+'\n')
    (work/'release.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
    # Query all releases rather than treating a transient release-view error as absence.
    pages = json.loads(gh('api','--paginate','--slurp',f'repos/{repo}/releases?per_page=100'))
    releases = [release for page in pages for release in page]
    existing = next((item for item in releases if item['tag_name'] == tag),None)
    if existing is None:
        gh('release','create',tag,'--repo',repo,'--draft','--title','Ю '+version,
           '--target',os.environ['GITHUB_SHA'],'--notes',config['notes'])
    if existing is None or existing['draft']:
        gh('release','upload',tag,str(apk),str(work/'SHA256SUMS'),str(work/'release.json'),'--repo',repo,'--clobber')
        gh('release','edit',tag,'--repo',repo,'--draft=false','--latest')
    else:
        assert not existing['prerelease'], 'Release is a prerelease'
    verify = work/'downloaded';verify.mkdir()
    gh('release','download',tag,'--repo',repo,'--pattern',filename,'--dir',str(verify))
    assert digest(verify/filename) == expected, 'Published APK differs from verified APK'
    # Publish discovery only after the public APK is available and verified.
    import base64
    body = {'message':'Update release feed to '+version,
            'content':base64.b64encode((work/'release.json').read_bytes()).decode(), 'branch':'main'}
    if current: body['sha'] = current['sha']
    if not current or previous != manifest:
        request = work/'request.json';request.write_text(json.dumps(body))
        gh('api','--method','PUT',feed_path,'--input',str(request))
    print('Published and verified:',manifest['apkUrl'])


if __name__ == '__main__':
    main()
