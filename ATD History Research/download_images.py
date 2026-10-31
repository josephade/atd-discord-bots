"""Cache archive image attachments locally for review; no Discord writes."""
import concurrent.futures
import json
from pathlib import Path
import requests
from PIL import Image, ImageOps, ImageDraw, ImageFont

BASE=Path(__file__).resolve().parent/'raw'
messages=json.loads((BASE/'historic-channel.json').read_text(encoding='utf-8'))['messages']
images=[]
for n,m in enumerate(messages,1):
    for j,a in enumerate(m['attachments'],1):
        if (a.get('type') or '').startswith('image/') or a['name'].lower().endswith(('.png','.jpg','.jpeg')):
            images.append({'moment':n,'attachment':j,'caption':m['content'],'source':m['url'],
                'url':a['url'],'path':str(BASE/f'moment-{n}-{j}.png')})

def download(item):
    target=Path(item['path'])
    old=BASE/f"moment-{item['moment']}.png"
    if target.exists():return True
    if item['attachment']==1 and old.exists():target.write_bytes(old.read_bytes());return True
    try:
        r=requests.get(item['url'],timeout=30);r.raise_for_status()
        target.write_bytes(r.content)
        return True
    except requests.RequestException:return False

with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
    for item,success in zip(images,pool.map(download,images)):item['downloaded']=success
(BASE/'image-index.json').write_text(json.dumps(images,indent=2),encoding='utf-8')
print(f"Cached {sum(x['downloaded'] for x in images)} of {len(images)} image attachments.")
selected=[7,11,18,38,51,52,54,60,61,62,64,69,75,82,84,85,86,87,88,91,99,100,103,108,111,113,114,115,121,122,123,130]
font=ImageFont.truetype(r'C:\Windows\Fonts\segoeuib.ttf',18)
for page,start in enumerate(range(0,len(selected),4),1):
    canvas=Image.new('RGB',(1600,1800),'#f0f0f0');draw=ImageDraw.Draw(canvas)
    for k,n in enumerate(selected[start:start+4]):
        item=next((x for x in images if x['moment']==n and x['downloaded']),None)
        if not item:continue
        picture=Image.open(item['path']).convert('RGB')
        thumb=ImageOps.contain(picture,(780,810))
        x=(k%2)*800;y=(k//2)*900
        draw.text((x+10,y+5),f'MOMENT {n}: '+item['caption'][:65],font=font,fill='#111111')
        canvas.paste(thumb,(x+10,y+50))
    canvas.save(BASE/f'review-{page}.jpg',quality=94)
print('Created eight four-image review sheets.')
