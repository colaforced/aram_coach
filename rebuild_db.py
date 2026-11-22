import json, sys
sys.stdout.reconfigure(encoding='utf-8')

cm = json.load(open('data/champion_map.json', encoding='utf-8'))
cd = json.load(open('data/champions.json', encoding='utf-8'))

NAME_ALIASES = {
    '诺手': '诺克萨斯之手', '德莱厄斯': '诺克萨斯之手',
    '大嘴': '克格莫', '克格莫': '克格莫',
    '蒙多': '蒙多医生',
    '铁男': '铁铠冥魂',
    '石头人': '熔岩巨兽',
    '卡兹克': '虚空掠夺者',
    '炸弹人': '爆破鬼才', '吉格斯': '爆破鬼才',
    'EZ': '探险家', 'ez': '探险家',
    '薇恩': '暗夜猎手',
    '卡萨丁': '虚空行者',
    '乐芙兰': '诡术妖姬',
    '索拉卡': '众星之子',
    '娑娜': '琴瑟仙女',
    '迦娜': '风暴之怒',
    '泽拉斯': '远古巫灵',
    '阿卡丽': '离群之刺',
    '卡特琳娜': '不祥之刃',
    '凯特琳': '皮城女警',
    '亚索': '疾风剑豪',
    '梦魇': '永恒梦魇',
    '木木': '殇之木乃伊',
    '蔚': '皮城执法官',
    '韦鲁斯': '惩戒之箭',
    '卢锡安': '圣枪游侠',
    '派克': '血港鬼影', ' 派克': '血港鬼影',
    '卡莎': '虚空之女',
}

map_name_to_id = {v: k for k, v in cm.items()}

new_db = {}
matched = 0
unmatched = []

for old_key, data in cd.items():
    name = data.get('name', '')
    std_name = NAME_ALIASES.get(name, name)
    
    correct_id = map_name_to_id.get(std_name)
    if not correct_id:
        correct_id = map_name_to_id.get(name)
    
    if correct_id:
        data['name'] = cm[correct_id]
        new_db[correct_id] = data
        matched += 1
    else:
        unmatched.append(f'{old_key}: {name}')

for cid, cname in sorted(cm.items(), key=lambda x: int(x[0])):
    if cid not in new_db:
        role = '战士'
        new_db[cid] = {
            'name': cname,
            'role': role,
            'primary_rune': '',
            'primary_branch': '',
            'primary_details': [],
            'secondary_rune': '',
            'secondary_branch': '',
            'build': ['三相之力', '斯特拉克的挑战护手', '死亡之舞', '守护天使', '适应性头盔', '水银之靴'],
            'core_cost': 3000,
            'situational': {},
            'tips': [f'大乱斗{cname}核心是雪球接技能', '注意走位躲关键控制', '残血别贪线先回家'],
            'snowball_tip': '雪球拉脸接主要技能',
            'flash_tip': '闪现用来躲技能或追击残血',
            'aram_strength': 55,
        }

new_db_sorted = {k: new_db[k] for k in sorted(new_db.keys(), key=lambda x: int(x))}

with open('data/champions.json', 'w', encoding='utf-8') as f:
    json.dump(new_db_sorted, f, ensure_ascii=False, indent=2)

print(f'匹配成功: {matched}')
print(f'用ID自动补全: {len(new_db) - matched}')
print(f'总条目: {len(new_db)}')
if unmatched:
    print(f'仍无法匹配: {unmatched[:10]}')

print()
print('=== 验证关键英雄 ===')
for cid in ['31', '122', '54', '14', '17', '235']:
    d = new_db.get(cid, {})
    print(f'key={cid} -> name={d.get("name")} role={d.get("role")} tips0={str(d.get("tips",["?"]))[0][:20]}')
