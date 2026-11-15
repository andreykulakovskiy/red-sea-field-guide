#!/usr/bin/env python3
"""Build an offline Russian Red Sea photo field guide from Wikimedia Commons.
Only freely redistributable CC/PD photos are accepted. Requires internet for fetching.
"""
import argparse, concurrent.futures, html, json, os, re, sys, time, unicodedata
from pathlib import Path
from urllib.parse import quote
import requests
from io import BytesIO
from photo_quality import bad_metadata, metadata_bonus, photo_score, ahash, similar
from PIL import Image, ImageOps
from reportlab.pdfgen import canvas
from reportlab.lib.colors import HexColor
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.utils import ImageReader, simpleSplit

ROOT=Path(__file__).resolve().parents[1]
DATA=ROOT/'data'; ASSET=ROOT/'assets'; OUT=ROOT/'output'
API='https://commons.wikimedia.org/w/api.php'
UA='OpenRedSeaFieldGuide/2.0 (open photo guide; https://github.com/andreykulakovskiy/red-sea-field-guide)'
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
    """Return licensed, correctly identified Wikimedia candidates; inspect pixels next."""
    scientific=species['scientific']; key=species['id']; fixed=curated.get(key,[])
    generic=any(k in scientific for k in ('spp.','sp.',' / ','Gorgonacea'))
    search=scientific.split(' spp.')[0].split(' sp.')[0].split(' / ')[0]
    term=search if generic else '"'+scientific+'"'
    params={'action':'query','format':'json','formatversion':'2','generator':'search',
            'gsrnamespace':'6','gsrsearch':term,'gsrlimit':'50',
            'prop':'imageinfo','iiprop':'url|size|mime|extmetadata','iiurlwidth':'1100'}
    pages=[]
    if fixed:
        pinned={'action':'query','format':'json','formatversion':'2',
                'titles':'|'.join('File:'+f for f in fixed),
                'prop':'imageinfo','iiprop':'url|size|mime|extmetadata','iiurlwidth':'1100'}
        try: pages.extend(get_json(pinned).get('query',{}).get('pages',[]))
        except Exception as ex: print('Curated query error:',scientific,str(ex),file=sys.stderr)
    try: pages.extend(get_json(params).get('query',{}).get('pages',[]))
    except Exception as ex: print('Search query error:',scientific,str(ex),file=sys.stderr)
    candidates=[];seen=set()
    first=scientific.split()[0].casefold(); exact=scientific.casefold()
    for page in pages:
        img=(page.get('imageinfo') or [None])[0]
        if not img or img.get('mime') not in ('image/jpeg','image/png','image/webp'): continue
        if min(img.get('width',0),img.get('height',0))<300: continue
        meta=img.get('extmetadata') or {}
        lic=acceptable_license(meta)
        if not lic: continue
        name=page.get('title','').removeprefix('File:')
        if not name or name in seen: continue
        seen.add(name)
        desc=clean_html(meta.get('ImageDescription',{}).get('value',''))+' '+clean_html(meta.get('ObjectName',{}).get('value',''))
        if bad_metadata(name,desc): continue
        n=normalize(name);d=normalize(desc)
        # A mere mention in unrelated description must not misidentify the subject.
        if name not in fixed:
            if generic:
                if normalize(search) not in n and normalize(search) not in d: continue
            elif exact not in n and not (first in n and exact in d):
                continue
        score=0.0
        if exact in n: score+=50
        elif normalize(search) in n: score+=24
        if name in fixed: score+=75
        score+=metadata_bonus(n+' '+d)
        score+=min(img.get('width',0),3000)/300
        score+=min(img.get('height',0),2500)/650
        candidates.append((score,{'filename':name,
            'source_page':'https://commons.wikimedia.org/wiki/File:'+quote(name.replace(' ','_')),
            'remote_url':img.get('thumburl') or img.get('url'),
            'author':clean_html(meta.get('Artist',{}).get('value','')) or 'See source file',
            'license':lic,'license_url':clean_html(meta.get('LicenseUrl',{}).get('value','')),
            'attribution':clean_html(meta.get('Attribution',{}).get('value','')),
            'species_id':key,'scientific':scientific,'section':species['section'],
            'prior_score':round(score,1),
            'transformations':'Resized to max 1360 px; original aspect ratio preserved.'}))
    candidates.sort(key=lambda x:-x[0])
    return key,[row for _,row in candidates[:max(8,per_species*3)]]


def download_one(candidate):
    """Fetch pixels before acceptance. Never reuse v0.1's stale image cache."""
    resp=requests.get(candidate['remote_url'],headers={'User-Agent':UA},timeout=60)
    resp.raise_for_status()
    if len(resp.content)>20*1024*1024: raise ValueError('Image too large')
    im=Image.open(BytesIO(resp.content)).convert('RGB')
    score,metrics=photo_score(im,candidate['section'],candidate['filename'])
    return score,candidate,im,metrics,ahash(im)


def fetch_photos(species,per_species):
    ASSET.mkdir(exist_ok=True);(ASSET/'v2').mkdir(exist_ok=True);OUT.mkdir(exist_ok=True)
    curated=json.loads((DATA/'curated.json').read_text('utf-8'))
    photos={};errors=[]
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        futures=[pool.submit(find_images,item,per_species,curated) for item in species]
        for fut in concurrent.futures.as_completed(futures):
            try: k,files=fut.result();photos[k]=files
            except Exception as e:errors.append(str(e))
    attached={s['id']:[] for s in species};qa=[]
    # Requests stay sequential to avoid hammering Commons thumbnail servers.
    for idx,sp in enumerate(species):
        choices=[]
        for p in photos.get(sp['id'],[]):
            try:
                score,meta,im,metrics,hashv=download_one(p)
                qa.append({'species':sp['scientific'],'filename':p['filename'],
                    'score':score,'status':'candidate' if score>=35 else 'rejected_low_quality',
                    **metrics})
                if score>=35:
                    choices.append((score+p['prior_score']*0.18,meta,im,metrics,hashv))
            except Exception as e:
                print('Photo skipped:',sp['scientific'],p['filename'],str(e)[:100],file=sys.stderr)
        choices.sort(key=lambda row:-row[0]);accepted=[]
        for score,meta,im,metrics,hashv in choices:
            if any(similar(hashv,other[4]) for other in accepted):continue
            accepted.append((score,meta,im,metrics,hashv))
            if len(accepted)>=per_species:break
        for j,(score,meta,im,metrics,hashv) in enumerate(accepted):
            path=ASSET/'v2'/(safe_id(sp['id'])+'-'+str(j+1)+'.jpg')
            im.thumbnail((1360,1360))
            im.save(path,'JPEG',quality=88,optimize=True)
            meta['local_path']=str(path.relative_to(ROOT))
            meta['quality_score']=round(score,1)
            meta['quality_metrics']=metrics
            attached[sp['id']].append(meta)
        print(f'[{idx+1}/{len(species)}] {sp["scientific"]}: {len(attached[sp["id"]])} verified natural photos',flush=True)
    (DATA/'manifest.json').write_text(json.dumps(attached,ensure_ascii=False,indent=2),encoding='utf-8')
    import csv
    with open(OUT/'PHOTO_QA.csv','w',encoding='utf-8-sig',newline='') as f:
        cols=['species','filename','status','score','saturation','blue','paper','black','gray','contrast','edges','width','height']
        wr=csv.DictWriter(f,fieldnames=cols);wr.writeheader()
        for row in qa:wr.writerow({col:row.get(col,'') for col in cols})
    print('Downloaded and selected images:',sum(map(len,attached.values())),'of',len(species),'entries;',len(errors),'search errors')
    return attached


def safe_id(s):
    return re.sub(r'[^a-z0-9-]+','-',s.lower()).strip('-')


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
    c.setTitle('Красное море: яркий офлайн-определитель v0.2, Faraana, Temple, Ras Katy')
    c.setAuthor('Open Red Sea Field Guide contributors')
    def page_header(title,page):
        c.setFillColor(HexColor(C_NAVY));c.rect(0,H-82,W,82,fill=1,stroke=0)
        text(c,28,H-36,title,19,'Bold','#FFFFFF')
        text(c,28,H-57,'Faraana Reef   •   Temple   •   Ras Katy',9,'Body','#AEE6E5')
        c.setStrokeColor(HexColor('#D5E5E8'));c.line(24,36,W-24,36)
        text(c,25,22,'КРАСНОЕ МОРЕ  •  UNDERWATER FIELD GUIDE  •  CC-licensed photography',8,'Body',C_MUTED)
        text(c,W-45,22,str(page),8,'Bold',C_TEAL)
    page=1
    c.setFillColor(HexColor(C_NAVY));c.rect(0,0,W,H,fill=1,stroke=0)
    # Real Wikimedia photography, never AI-generated; favor brightly colored fish.
    options=[]
    for item in species:
        if item['section'] not in ('fish','coral'):continue
        for ph in manifest.get(item['id'],[]):
            path=ROOT/ph.get('local_path','')
            if path.exists():
                priority=12 if item['id'] in ('pomacanthus-imperator','pygoplites-diacanthus','aetobatus-ocellatus') else 0
                options.append((ph.get('quality_score',0)+priority,path,ph))
    options.sort(key=lambda a:-a[0])
    if options:
        with Image.open(options[0][1]) as im:
            cover=ImageOps.fit(im.convert('RGB'),(1100,850),method=Image.Resampling.LANCZOS)
            c.drawImage(ImageReader(cover),0,250,width=W,height=450)
    c.setFillColor(HexColor(C_NAVY));c.rect(0,700,W,142,fill=1,stroke=0)
    c.setFillColor(HexColor(C_NAVY));c.rect(0,0,W,250,fill=1,stroke=0)
    text(c,42,754,'КРАСНОЕ МОРЕ',35,'Bold','#FFFFFF')
    text(c,43,721,'ЖИВОЙ ПОЛЕВОЙ ОПРЕДЕЛИТЕЛЬ',13,'Body','#BCF0EC')
    text(c,42,210,'Faraana  /  Temple  /  Ras Katy  /  Рас-Ум-Сид',11,'Bold','#A9E3DD')
    text(c,42,172,f'{len(species)} видов и групп  •  настоящие подводные фотографии',11,'Bold','#FFFFFF')
    wrapped(c,'Кораллы, рыбы, скаты, моллюски и ракообразные. Иллюстрированные карточки, места обитания, признаки и авторство каждого фото.',42,138,510,10.4,16,3,'#E1F3F1')
    text(c,42,48,'OFFLINE  •  OPEN SOURCE  •  v0.2',10,'Bold','#F9CF86')
    c.showPage();page+=1
    page_header('Как пользоваться',page)
    blocks=[('Плато Faraana','Проверяй молодые колонии, моллюсков, хирургов, спинорогов. Не наступай на риф.'),('Temple','Колонны и щели: крылатки, мягкие кораллы, антиасы, укрытия мурен.'),('Ras Katy','Песчаные окна: бычки с креветками, скаты, рыбы-крокодилы.'),('У внешней стенки','Возможны наполеоны, черепахи, орляки; манта — редкая удача в открытой воде.')]
    y=685
    for a,b in blocks:
        text(c,32,y,a,14,'Bold',C_TEAL);wrapped(c,b,32,y-22,525,11,16,3);y-=120
    wrapped(c,'Безопасность: сильные течения бывают даже при гладкой воде. Не касайся кораллов, моллюсков и рыб. За мысами и возле катеров не плавай в одиночку; используй буй.',32,194,520,11,17,5,color='#B94841')
    c.showPage();page+=1
    # Extra full-color pages, using the same attribution-preserving in-situ images.
    highlights=[('acropora-spp','СИНИЕ АКРОПОРЫ'),('mobula-birostris','ОКЕАНИЧЕСКАЯ МАНТА'),
                ('octopus-cyanea','ОСЬМИНОГ'),('aetobatus-ocellatus','ОРЛЯК'),
                ('tridacna-maxima','ТРИДАКНА'),('pterois-miles','КРЫЛАТКА')]
    shown=[]
    for species_id,label in highlights:
        found=next((v for v in species if v['id']==species_id),None)
        if not found:continue
        photos=[p for p in manifest.get(species_id,[]) if (ROOT/p.get('local_path','')).exists()]
        if photos:shown.append((found,label,photos[0]))
    for k in range(0,len(shown),2):
        page_header('МОРСКИЕ ВСТРЕЧИ • ФОТОГАЛЕРЕЯ',page)
        for j,(found,label,ph) in enumerate(shown[k:k+2]):
            y=H-125-j*350
            c.setFillColor(HexColor('#E5F2F2'));c.roundRect(26,y-320,W-52,318,12,fill=1,stroke=0)
            image_fit(c,ROOT/ph['local_path'],35,y-249,525,239)
            text(c,40,y-275,label,15,'Bold',C_NAVY)
            text(c,40,y-295,found['scientific'],10,'Italic',C_TEAL)
            text(c,40,y-311,'Фото: '+ph['author'][:64],7,'Body',C_MUTED)
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
                photo_width=165 if len(srcs)>2 else (252 if len(srcs)>1 else 510)
                if srcs:
                    # image cards have fixed footprint and cannot overlap the title/description
                    start_x=38
                    for pi,photo in enumerate(srcs[:3]):
                        box_x=start_x+pi*(photo_width+9)
                        c.setFillColor(HexColor('#DDE9E9'));c.roundRect(box_x,top-229,photo_width,165,7,stroke=0,fill=1)
                        image_fit(c,ROOT/photo['local_path'],box_x+4,top-225,photo_width-8,157)
                        text(c,box_x+1,top-241,f'Фото {pi+1}: {photo["author"][:27]}',7,'Body',C_MUTED)
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
    ap.add_argument('--photos',type=int,choices=[1,2,3],default=3,help='Photos per species (default 3).')
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
