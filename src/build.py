#!/usr/bin/env python3
"""Build an offline Russian Red Sea photo field guide from Wikimedia Commons.
Only freely redistributable CC/PD photos are accepted. Requires internet for fetching.
"""
import argparse, concurrent.futures, html, json, os, re, sys, time, unicodedata
from pathlib import Path
from urllib.parse import quote
import requests
from PIL import Image, ImageOps
from reportlab.pdfgen import canvas
from reportlab.lib.colors import HexColor
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.utils import ImageReader, simpleSplit

ROOT=Path(__file__).resolve().parents[1]
DATA=ROOT/'data'; ASSET=ROOT/'assets'; OUT=ROOT/'output'
API='https://commons.wikimedia.org/w/api.php'
UA='OpenRedSeaFieldGuide/1.0 (educational, attribution-preserving image reuse; contact: https://github.com/)'
ALLOW=['CC BY-SA','CC BY ','CC0','PUBLIC DOMAIN','PDM','PD-']
FONT_DIRS=['/usr/share/fonts/truetype/dejavu','/usr/share/fonts/truetype/noto','/Library/Fonts','C:/Windows/Fonts']
C_NAVY='#082C3A'; C_TEAL='#017D83'; C_INK='#1C3740'; C_MUTED='#49666D'; C_BG='#F0F7F7'


def register_fonts():
    trials=[('DejaVuSans','DejaVuSans.ttf','DejaVuSans-Bold.ttf','DejaVuSans-Oblique.ttf'),('NotoSans','NotoSans-Regular.ttf','NotoSans-Bold.ttf','NotoSans-Italic.ttf')]
    for _,reg,bold,ital in trials:
        for base in FONT_DIRS:
            bd=Path(base)
            if (bd/reg).exists() and (bd/bold).exists():
                pdfmetrics.registerFont(TTFont('Body',str(bd/reg)))
                pdfmetrics.registerFont(TTFont('Bold',str(bd/bold)))
                pdfmetrics.registerFont(TTFont('Italic',str(bd/ital if (bd/ital).exists() else bd/reg)))
                return
    raise RuntimeError('Install DejaVu Sans or Noto Sans fonts (Cyrillic required).')


def get_json(params):
    for attempt in range(4):
        try:
            r=requests.get(API,params=params,headers={'User-Agent':UA},timeout=35)
            if r.status_code in (429,500,502,503):
                time.sleep(2+attempt*5); continue
            r.raise_for_status()
            return r.json()
        except (requests.RequestException,ValueError):
            if attempt==3: raise
            time.sleep(1+attempt*3)
    raise RuntimeError('MediaWiki API is temporarily unavailable')


def clean_html(s):
    s=re.sub(r'<[^>]+>',' ',str(s or ''))
    return re.sub(r'\s+',' ',html.unescape(s)).strip()


def normalize(s):
    return re.sub(r'\s+',' ',str(s).replace('_',' ').casefold()).strip()


def acceptable_license(meta):
    name=clean_html(meta.get('LicenseShortName',{}).get('value',''))
    name_norm=name.upper()
    return name if any(x in name_norm for x in ALLOW) else None


def find_images(species, per_species, curated):
    scientific=species['scientific']
    key=species['id']
    fixed=curated.get(key,[])
    search=scientific.split(' spp.')[0].split(' sp.')[0].split(' / ')[0]
    if ' ' in scientific and not any(x in scientific for x in ('spp.','sp.','/')):
        term='"'+scientific+'"'
    else: term=search
    params={'action':'query','format':'json','formatversion':'2','generator':'search',
        'gsrnamespace':'6','gsrsearch':term,'gsrlimit':'30',
        'prop':'imageinfo','iiprop':'url|size|mime|extmetadata','iiurlwidth':'940'}
    pages=[]
    if fixed:
        pinned={'action':'query','format':'json','formatversion':'2','titles':'|'.join('File:'+f for f in fixed),
        'prop':'imageinfo','iiprop':'url|size|mime|extmetadata','iiurlwidth':'940'}
        try: pages.extend(get_json(pinned).get('query',{}).get('pages',[]))
        except Exception as ex: print('Curated query error',scientific,str(ex),file=sys.stderr)
    try: pages.extend(get_json(params).get('query',{}).get('pages',[]))
    except Exception as ex: print('Search query error',scientific,str(ex),file=sys.stderr)
    candidates=[]
    for p in pages:
        img=(p.get('imageinfo') or [None])[0]
        if not img or not (img.get('thumburl') or img.get('url')): continue
        if img.get('mime') not in ('image/jpeg','image/png','image/webp'): continue
        meta=img.get('extmetadata') or {}
        lic=acceptable_license(meta)
        if not lic: continue
        name=p.get('title','')[5:]
        n=normalize(name)
        if re.search(r'(drawing|illustration|diagram|lineart|clipart|artwork|map|sketch|logo|vector)',n): continue
        if ' '.join(scientific.split()[:2]).casefold() not in n and 'spp.' not in scientific and 'sp.' not in scientific and '/' not in scientific:
            # only accept accurately named species photos (not lookalikes)
            continue
        if ('spp.' in scientific or 'sp.' in scientific) and normalize(search) not in n: continue
        score=0
        if normalize(scientific) in n: score+=60
        if normalize(search) in n: score+=20
        if any(t in n for t in ('red sea','mar rojo','ras katy','sharm','egypt','egipto','ras muhammad')): score+=15
        if name in fixed: score+=1000
        score+=min(img.get('width',0),2400)/600
        candidates.append((score,name,img,meta,lic))
    candidates.sort(key=lambda x:-x[0]); selected=[]; seen=set()
    for score,name,img,meta,lic in candidates:
        if name in seen: continue
        seen.add(name)
        selected.append({'filename':name,'source_page':'https://commons.wikimedia.org/wiki/File:'+quote(name.replace(' ','_')),
            'remote_url':img.get('thumburl') or img.get('url'),
            'author':clean_html(meta.get('Artist',{}).get('value','')) or 'See source file',
            'license':lic,
            'license_url':clean_html(meta.get('LicenseUrl',{}).get('value','')),
            'attribution':clean_html(meta.get('Attribution',{}).get('value','')),
            'species_id':key,'scientific':scientific,
            'transformations':'Resized to max 1100 px; original aspect preserved.'})
        if len(selected)>=per_species: break
    return key,selected


def safe_id(s):
    return re.sub(r'[^a-z0-9-]+','-',s.lower()).strip('-')


def download_one(photo,seq):
    key=photo['species_id']; target=ASSET/(safe_id(key)+'-'+str(seq+1)+'.jpg')
    photo['local_path']=str(target.relative_to(ROOT))
    if target.exists() and target.stat().st_size>1000: return photo
    resp=requests.get(photo['remote_url'],headers={'User-Agent':UA},timeout=55)
    resp.raise_for_status()
    if len(resp.content)>18*1024*1024: raise ValueError('Image too large')
    from io import BytesIO
    im=Image.open(BytesIO(resp.content)).convert('RGB')
    im.thumbnail((1100,1100))
    im.save(target,'JPEG',quality=82,optimize=True)
    return photo


def fetch_photos(species,per_species):
    ASSET.mkdir(exist_ok=True);OUT.mkdir(exist_ok=True)
    curated=json.loads((DATA/'curated.json').read_text('utf-8'))
    photos={}; errors=[]
    # A gentle 3-worker limit to avoid hammering the Commons API.
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        future=[pool.submit(find_images,item,per_species,curated) for item in species]
        for fut in concurrent.futures.as_completed(future):
            try: k,files=fut.result();photos[k]=files
            except Exception as e:errors.append(str(e))
    downloads=[]
    for sp in species:
        for i,p in enumerate(photos.get(sp['id'],[])):
            try: downloads.append(download_one(p,i))
            except Exception as e: print('Download skipped',sp['scientific'],str(e),file=sys.stderr)
    attached={k:[] for k in (s['id'] for s in species)}
    for p in downloads: attached[p['species_id']].append(p)
    (DATA/'manifest.json').write_text(json.dumps(attached,ensure_ascii=False,indent=2),encoding='utf-8')
    print('Downloaded images:',len(downloads),'of',len(species),'entries;',len(errors),'search errors')
    return attached


def text(c,x,y,s,size=10,font='Body',color=C_INK):
    c.setFillColor(HexColor(color));c.setFont(font,size);c.drawString(x,y,str(s))


def wrapped(c,s,x,top,width,size=9,leading=13,max_lines=4,color=C_INK,font='Body'):
    lines=simpleSplit(str(s),font,size,width)
    for i,line in enumerate(lines[:max_lines]):text(c,x,top-i*leading,line,size,font,color)
    return len(lines[:max_lines])*leading


def image_fit(c,path,x,y,w,h):
    with Image.open(path) as im: iw,ih=im.size
    scale=min(w/iw,h/ih)
    ww,hh=iw*scale,ih*scale
    c.drawImage(str(path),x+(w-ww)/2,y+(h-hh)/2,ww,hh,preserveAspectRatio=True,mask='auto')


def build_pdf(species,manifest,dest):
    register_fonts();W,H=595,842
    c=canvas.Canvas(str(dest),pagesize=(W,H),pageCompression=1)
    c.setTitle('Красное море: офлайн-определитель Faraana, Temple, Ras Katy')
    c.setAuthor('Open Red Sea Field Guide contributors')
    def page_header(title,page):
        c.setFillColor(HexColor(C_NAVY));c.rect(0,H-82,W,82,fill=1,stroke=0)
        text(c,28,H-36,title,19,'Bold','#FFFFFF')
        text(c,28,H-57,'Faraana Reef   •   Temple   •   Ras Katy',9,'Body','#AEE6E5')
        c.setStrokeColor(HexColor('#D5E5E8'));c.line(24,36,W-24,36)
        text(c,25,22,'Красное море   |   реальные фото Wikimedia Commons',8,'Body',C_MUTED)
        text(c,W-45,22,str(page),8,'Bold',C_TEAL)
    page=1
    c.setFillColor(HexColor(C_NAVY));c.rect(0,0,W,H,fill=1,stroke=0)
    text(c,42,728,'КРАСНОЕ МОРЕ',37,'Bold','#FFFFFF')
    text(c,43,684,'Полевой определитель рифовой жизни',16,'Body','#BCF0EC')
    text(c,43,645,'Faraana • Temple • Ras Katy • Рас-Ум-Сид',12,'Body','#FFFFFF')
    text(c,44,528,f'{len(species)} видов и групп • офлайн-фотографии • свободные лицензии',12,'Body','#FFFFFF')
    wrapped(c,'Фотографии вставлены непосредственно в PDF. Интернет не нужен. Авторы и лицензии перечислены в конце; полная информация приложена в файле PHOTO_CREDITS.csv.',44,455,510,11,18,6,'#D7F1F0')
    wrapped(c,'Материал предназначен для наблюдений, не для точной идентификации сложных видов без проверки специалистом.',44,342,500,10,17,4,'#D7F1F0')
    c.showPage();page+=1
    page_header('Как пользоваться',page)
    blocks=[('Плато Faraana','Проверяй молодые колонии, моллюсков, хирургов, спинорогов. Не наступай на риф.'),('Temple','Колонны и щели: крылатки, мягкие кораллы, антиасы, укрытия мурен.'),('Ras Katy','Песчаные окна: бычки с креветками, скаты, рыбы-крокодилы.'),('У внешней стенки','Возможны наполеоны, черепахи, орляки; манта — редкая удача в открытой воде.')]
    y=685
    for a,b in blocks:
        text(c,32,y,a,14,'Bold',C_TEAL);wrapped(c,b,32,y-22,525,11,16,3);y-=120
    wrapped(c,'Безопасность: сильные течения бывают даже при гладкой воде. Не касайся кораллов, моллюсков и рыб. За мысами и возле катеров не плавай в одиночку; используй буй.',32,194,520,11,17,5,color='#B94841')
    c.showPage();page+=1
    section_order=[('coral','КОРАЛЛЫ И СТРЕКАЮЩИЕ'),('fish','РЫБЫ И СКАТЫ'),('mollusc','МОЛЛЮСКИ'),('crust','РАКООБРАЗНЫЕ')]
    code_to_title=dict(section_order)
    # One large photo per card, sometimes two; two cards per page for easy phone reading.
    for sec,heading in section_order:
        members=[s for s in species if s['section']==sec]
        for start in range(0,len(members),2):
            page_header(heading,page)
            for ind,entry in enumerate(members[start:start+2]):
                top=H-102-ind*355
                c.setFillColor(HexColor('#F1F7F7'));c.roundRect(25,top-330,W-50,328,13,stroke=0,fill=1)
                text(c,37,top-31,entry['ru'],15,'Bold',C_NAVY)
                text(c,37,top-50,entry['scientific'],10,'Italic',C_TEAL)
                srcs=[p for p in manifest.get(entry['id'],[]) if (ROOT/p.get('local_path','')).exists()]
                photo_width=245 if len(srcs)>1 else 350
                if srcs:
                    # image cards have fixed footprint and cannot overlap the title/description
                    start_x=38
                    for pi,photo in enumerate(srcs[:2]):
                        box_x=start_x+pi*(photo_width+9)
                        c.setFillColor(HexColor('#DDE9E9'));c.roundRect(box_x,top-229,photo_width,165,7,stroke=0,fill=1)
                        image_fit(c,ROOT/photo['local_path'],box_x+4,top-225,photo_width-8,157)
                        text(c,box_x+1,top-241,f'Фото {pi+1}: {photo["author"][:45]}',7,'Body',C_MUTED)
                else:
                    c.setFillColor(HexColor('#E4ECEC'));c.roundRect(38,top-231,516,165,8,stroke=0,fill=1)
                    wrapped(c,'Нет проверенной свободной фотографии. Не подменяем её иллюстрацией другого вида.',59,top-143,460,11,16,3,C_MUTED)
                wrapped(c,entry['hook'],38,top-270,507,9.5,13,3)
                wrapped(c,'Где искать: '+entry['where'],38,top-309,507,9,12,2,C_MUTED)
                if entry.get('flag'):text(c,437,top-50,entry['flag'][:16],8,'Bold','#B05D25')
            c.showPage();page+=1
    page_header('Авторские права на фотографии',page)
    wrapped(c,'Код генератора распространяется по MIT, текст справочника — CC BY-SA 4.0. Каждая фотография остаётся под своей лицензией, указанной в списке ниже и в PHOTO_CREDITS.csv. Фото были уменьшены до 1100 px для PDF.',28,728,536,9.5,14,6)
    y=632
    for entry in species:
        for p in manifest.get(entry['id'],[]):
            if not (ROOT/p.get('local_path','')).exists():continue
            rows=[entry['scientific']+' — '+p['author'][:60],p['license']+' | '+p['source_page']]
            if y<90:
                c.showPage();page+=1;page_header('Авторы и лицензии (продолжение)',page);y=728
            y-=16
            for row in rows:
                for line in simpleSplit(row,'Body',7.4,530)[:3]:
                    text(c,28,y,line,7.4,'Body',C_MUTED);y-=10
            y-=4
    c.showPage();c.save()


def credits_csv(manifest):
    import csv
    with open(OUT/'PHOTO_CREDITS.csv','w',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=['scientific','filename','author','license','license_url','source_page','local_path','transformations'])
        writer.writeheader()
        for rows in manifest.values():
            for item in rows:writer.writerow({k:item.get(k,'') for k in writer.fieldnames})


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--photos',type=int,choices=[1,2],default=2,help='Photos per species (default 2).')
    ap.add_argument('--offline',action='store_true',help='Do not access Wikimedia; build with cached photos only.')
    ap.add_argument('--limit',type=int,default=0,help='For testing with only first N entries')
    args=ap.parse_args()
    species=json.loads((DATA/'species.json').read_text('utf-8'))
    if args.limit:species=species[:args.limit]
    ASSET.mkdir(exist_ok=True);OUT.mkdir(exist_ok=True)
    if args.offline:
        manifest=json.loads((DATA/'manifest.json').read_text('utf-8')) if (DATA/'manifest.json').exists() else {}
    else: manifest=fetch_photos(species,args.photos)
    path=OUT/'Red_Sea_Field_Guide_OFFLINE_PHOTOS_RU.pdf'
    build_pdf(species,manifest,path)
    credits_csv(manifest)
    print('Created:',path, 'size:',round(path.stat().st_size/1024/1024,1),'MB')
    count=sum(len([p for p in manifest.get(s['id'],[]) if (ROOT/p.get('local_path','')).exists()]) for s in species)
    print('Embedded photographs:',count)
    if count==0: print('WARNING: PDF has no photographs. Run without --offline on an Internet-connected machine.')

if __name__=='__main__':main()
