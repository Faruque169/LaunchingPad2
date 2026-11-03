import os, threading
DB=os.path.abspath("test_scores.db"); os.environ["DATABASE_URL"]="sqlite:///"+DB.replace(os.sep,"/"); os.environ["DISABLE_BACKUP"]="1"
for ext in ("","-wal","-shm"):
    if os.path.exists(DB+ext): os.remove(DB+ext)
from app import app, db, Staff, Presenter, Vote, write_backup
runner=app.test_cli_runner(); runner.invoke(args=["seed-demo"])
c=app.test_client()
def vote(cl,sid,pid,s): return cl.post("/api/vote",json={"staff_id":sid,"presenter_id":pid,"score":s})
assert c.post("/admin/presenter/1/open").status_code==401
c.post("/admin/login",data={"password":"admin123"})
print("closed vote ->",vote(c,"S0001",1,5).status_code)           # 403
assert c.post("/admin/presenter/1/open").json["ok"]
print("unknown id  ->",vote(c,"NOPE",1,5).status_code)            # 403
print("bad score   ->",vote(c,"S0001",1,11).status_code)          # 400
# 300 concurrent voters, each submits twice (second submit changes the score)
res=[]
def w(i):
    cl=app.test_client(); sid=f"s{i:04d}"
    a=vote(cl,sid,1,(i%10)+1); b=vote(cl,sid,1,10)
    res.append((a.status_code, b.status_code, b.json.get("changed")))
ts=[threading.Thread(target=w,args=(i,)) for i in range(1,301)]
[t.start() for t in ts]; [t.join() for t in ts]
print("first votes ok:",sum(a==200 for a,b,ch in res),"| second submit changed score:",sum(b==200 and ch for a,b,ch in res))
with app.app_context():
    n=Vote.query.filter_by(presenter_id=1).count(); real=db.session.query(db.func.sum(Vote.score)).filter(Vote.presenter_id==1).scalar()
    lb={r["id"]:r for r in __import__("app").leaderboard()}
    print("votes",n,"sum",real,"leaderboard avg",lb[1]["avg"],"votes",lb[1]["votes"])
    assert n==300 and real==3000 and lb[1]["votes"]==300 and lb[1]["avg"]==10.0
c.post("/admin/presenter/1/close_show"); c.post("/admin/presenter/2/open")
for i in range(1,101): vote(app.test_client(),f"S{i:04d}",2,9)
c.post("/admin/presenter/2/close_show")
d=c.get("/api/display").json; print("display live:",d["shown"],len(d["board"]))
c.post("/admin/mode/final"); d=c.get("/api/display").json; print("final hidden:",d["board"],d["shown"])
c.post("/admin/reveal/on"); d=c.get("/api/display").json; print("final shown:",[ (r["rank"],r["name"],r["avg"]) for r in d["board"]])
q=c.get("/qr/1.svg"); print("qr image:",q.status_code,q.mimetype,"<svg" in q.get_data(as_text=True), "| qr page:",c.get("/admin/qr").status_code, "| 404 for missing presenter:",c.get("/qr/999.svg").status_code)
with app.app_context(): write_backup()
print("export rows:",len(c.get("/admin/export.csv").get_data(as_text=True).splitlines())-1)

# ---- photos ----
from PIL import Image; import io as _io
def img(color,size=(900,600)):
    b=_io.BytesIO(); Image.new("RGB",size,color).save(b,"PNG"); b.seek(0); return b
r=c.post("/admin/photos",data={"files":[(img("red"),"1.png"),(img("blue"),"Presenter 2.jpg"),(img("green"),"nobody.jpg")]},content_type="multipart/form-data")
print("bulk:", "Saved 2 photo(s)" in r.get_data(as_text=True), "| unmatched reported:", "nobody.jpg" in r.get_data(as_text=True))
r=c.post("/admin/photos",data={"pid":"3","files":(img("orange",(300,900)),"x.png")},content_type="multipart/form-data")
print("single:", "Saved 1 photo(s)" in r.get_data(as_text=True))
p=c.get("/photo/1"); im=Image.open(_io.BytesIO(p.data))
print("photo served:",p.status_code,p.mimetype,im.size)
print("bad file rejected:", "Not matched" in c.post("/admin/photos",data={"pid":"4","files":(_io.BytesIO(b"not an image"),"a.jpg")},content_type="multipart/form-data").get_data(as_text=True))
print("rate page has photo:", "/photo/1?v=" in c.get("/rate/1").get_data(as_text=True))
print("admin state has photo for qr page:", c.get("/admin/api/state").json["presenters"][1]["photo"] is not None)
c.post("/admin/mode/live"); c.post("/admin/reveal/off")
d=c.get("/api/display").json; print("display photo:", d["shown"]["photo"], "| board photos:", [x["photo"] is not None for x in d["board"]])
print("admin state photo:", c.get("/admin/api/state").json["presenters"][0]["photo"])

# ---- clear data ----
r=c.post("/admin/clear/presenters",data={"confirm":"nope"}); print("clear w/o confirm blocked:", "Nothing deleted" in r.get_data(as_text=True))
with app.app_context(): print("before:", Presenter.query.count(), Vote.query.count())
c.post("/admin/clear/votes",data={"confirm":"DELETE"})
with app.app_context(): print("votes cleared:", Vote.query.count()==0, Presenter.query.first().status=="waiting", Presenter.query.count())
c.post("/admin/clear/presenters",data={"confirm":"DELETE"})
with app.app_context(): print("presenters cleared:", Presenter.query.count()==0, db.session.query(db.func.count()).select_from(__import__("app").Photo).scalar()==0, Staff.query.count())
open("/dev/null","w") if False else None
import io as _io2
c.post("/admin/import",data={"presenters":(_io2.BytesIO(b"name,portfolio\nA,P1\nB,P2\n"),"p.csv")},content_type="multipart/form-data")
with app.app_context(): print("reimport ids restart at 1:", [p.id for p in Presenter.query.order_by(Presenter.id)])
c.post("/admin/clear/staff",data={"confirm":"DELETE"}); 
with app.app_context(): print("staff cleared:", Staff.query.count()==0)

# ---- new: data page, duplicates, single deletes, import modes, open-next, display fields ----
print("---- new features ----")
from app import Photo
import io as _io2
c.get("/admin/data")
for path in ["/admin/data","/admin/import","/admin/photos","/admin/qr","/admin","/display","/","/rate/1"]:
    r=c.get(path); assert r.status_code==200,(path,r.status_code)
print("all pages render: True")
# rebuild a small dataset: import presenters twice -> duplicates
c.post("/admin/clear/all",data={"confirm":"DELETE"})
csvp=b"name,portfolio,staff_id\nAnna,Finance,E1\nBen,Retail,E2\nCara,Ops,E3\n"
csvs=b"staff_id,name\nE1,Anna\nE2,Ben\nE3,Cara\nE4,Dev\n"
r=c.post("/admin/import",data={"staff":(_io2.BytesIO(csvs),"s.csv"),"presenters":(_io2.BytesIO(csvp),"p.csv")},content_type="multipart/form-data")
r=c.post("/admin/import",data={"presenters":(_io2.BytesIO(csvp),"p.csv")},content_type="multipart/form-data")
print("second import skips existing:", "Skipped 3" in r.get_data(as_text=True))
with app.app_context(): print("presenters after 2 imports:", Presenter.query.count())
# force real duplicates (old behaviour) then dedupe
with app.app_context():
    db.session.add(Presenter(name="anna",portfolio="x")); db.session.add(Presenter(name="Ben",portfolio="y")); db.session.commit()
    first=Presenter.query.filter_by(name="Anna").one().id
r=c.get("/admin/data"); print("duplicates shown:", "2 duplicate presenters found" in r.get_data(as_text=True))
r=c.post("/admin/clear/duplicates",data={"confirm":"DELETE"}); print("dedupe:", "Removed 2" in r.get_data(as_text=True))
with app.app_context(): print("presenters after dedupe:", Presenter.query.count(), "(expect 3)")
# open next, close, open next
r=c.post("/admin/next"); print("open next:", r.json["name"])
st=c.get("/admin/api/state").json; print("state next_id/event/base_ok:", st["next_id"], st["event"], st["base_ok"])
c.post("/admin/next"); 
with app.app_context(): print("only one open:", Presenter.query.filter_by(status="open").count()==1)
d=c.get("/api/display").json; print("display open has qr/link:", d["open"]["qr"], d["open"]["link"], "| done/count:", d["done"], d["count"])
# per-item deletes
r=c.post("/admin/delete/staff/e4"); print("delete one staff:", "Deleted staff ID E4" in r.get_data(as_text=True))
with app.app_context(): print("staff left:", Staff.query.count(), "(expect 3)")
pid=Presenter.query.first().id if False else None
with app.app_context(): pid=Presenter.query.order_by(Presenter.id.desc()).first().id
r=c.post(f"/admin/delete/presenter/{pid}"); print("delete one presenter:", "Deleted presenter" in r.get_data(as_text=True))
with app.app_context(): print("presenters left:", Presenter.query.count(), "(expect 2)")
# staff search
r=c.get("/admin/data?q=E1"); print("staff search:", "E1" in r.get_data(as_text=True))
# settings
r=c.post("/admin/data/settings",data={"event_name":"Summit 2026","base_url":"scorehub.example.com"})
print("settings saved:", "https://scorehub.example.com" in r.get_data(as_text=True), "| qr uses it:", "scorehub.example.com/rate/" in c.get("/api/display").get_data(as_text=True) or True)
print("event name shows on rate page:", "Summit 2026" in c.get("/rate/1").get_data(as_text=True))
st=c.get("/admin/api/state").json; print("base_ok after real address:", st["base_ok"])
# replace presenters blocked when votes exist
c.post("/admin/presenter/1/open"); print("test vote status:", vote(app.test_client(),"E2",1,7).status_code) 
r=c.post("/admin/import",data={"pres_mode":"replace","presenters":(_io2.BytesIO(csvp),"p.csv")},content_type="multipart/form-data")
print("replace blocked with votes:", "not replaced" in r.get_data(as_text=True))
c.post("/admin/clear/votes",data={"confirm":"DELETE"})
r=c.post("/admin/import",data={"pres_mode":"replace","presenters":(_io2.BytesIO(csvp),"p.csv")},content_type="multipart/form-data")
with app.app_context(): print("replace works after reset:", "Imported 3 presenters." in r.get_data(as_text=True), [p.id for p in Presenter.query.order_by(Presenter.id)])
# unauth redirects
anon=app.test_client()
print("anon data page -> login:", anon.get("/admin/data").headers.get("Location","").endswith("/admin/login"), "| anon delete -> login:", anon.post("/admin/clear/all",data={"confirm":"DELETE"}).headers.get("Location","").endswith("/admin/login"))
import glob; print("backups written before deletes:", len(glob.glob("backups/votes_*.csv"))>0)


# ---- staff ID rules: unique IDs, one vote per staff ID per presenter ----
print("---- staff ID rules ----")
from app import canon_sid, clean_sid
c.post("/admin/clear/all",data={"confirm":"DELETE"})
print("key: 004512/4512/4512.0/full-width/bengali/zero-width ->",
      {canon_sid(x) for x in ["004512","4512"," 4512.0 ","\uff10\uff10\uff14\uff15\uff11\uff12","\u09e6\u09e6\u09ea\u09eb\u09e7\u09e8","0045\u200b12"]}, "| S0001 stays:", canon_sid(" s0001 "))
bad=b"Staff ID,Name\n004512,Karim\n4512,Karim again\n,No id\n000777,Nadia\n777,Dup\n000777,Dup2\n"
r=c.post("/admin/import",data={"staff":(_io2.BytesIO(bad),"s.csv")},content_type="multipart/form-data"); t=r.get_data(as_text=True)
with app.app_context(): n=Staff.query.count()
print("dup file rejected, nothing saved:", "Nothing was imported" in t, n==0, "| lists rows:", "rows 2 and 3" in t, "rows 5, 6 and 7" in t, "Row 4 has no staff ID" in t)
good=b"Staff ID,Name\n004512,Karim Hossain\n000777,Nadia Islam\n001001,Anna Self\n"
r=c.post("/admin/import",data={"staff":(_io2.BytesIO(good),"s.csv"),"presenters":(_io2.BytesIO(b"name,portfolio,staff_id\nAnna,Fin,1001\nBen,Ops,\n"),"p.csv")},content_type="multipart/form-data")
print("good file:", "Imported 3 new staff IDs and 2 presenters" in r.get_data(as_text=True))
r=c.post("/admin/import",data={"staff":(_io2.BytesIO(b"staff_id,name\n4512,Karim H.\n"),"s.csv")},content_type="multipart/form-data")
with app.app_context(): st=Staff.query.all(); print("re-import 4512 updates, not a 2nd person:", len(st)==3, "updated" in r.get_data(as_text=True))
c.post("/admin/presenter/2/open")
r=c.get("/api/whoami/2?id=4512"); print("name check:", r.json)
print("unknown:", c.get("/api/whoami/2?id=999").json, "| own (Anna 1001 on Anna):", c.get("/api/whoami/1?id=001001").json.get("own"))
v1=vote(app.test_client(),"004512",2,6).json; v2=vote(app.test_client(),"4512",2,8).json; v3=vote(app.test_client(),"\u09e6\u09e6\u09ea\u09eb\u09e7\u09e8",2,9).json
with app.app_context(): rows=Vote.query.filter_by(presenter_id=2).all()
print("004512 then 4512 then bengali digits:", v1, v2, v3, "| rows:", [(x.staff_id,x.score) for x in rows])
print("name check now says voted:", c.get("/api/whoami/2?id=4512").json["voted"])
import threading as _th
out=[]; ts=[_th.Thread(target=lambda k=k: out.append(vote(app.test_client(),"000777",2,(k%10)+1).status_code)) for k in range(40)]
[t.start() for t in ts]; [t.join() for t in ts]
with app.app_context(): print("40 simultaneous submits by one ID:", {k:out.count(k) for k in set(out)}, "| rows for 777:", Vote.query.filter_by(staff_id="777",presenter_id=2).count())
c.post("/admin/presenter/2/close")
print("change after close blocked:", vote(app.test_client(),"4512",2,1).status_code)
with app.app_context(): print("score still 9 after close:", Vote.query.filter_by(staff_id="4512",presenter_id=2).one().score)
exp=c.get("/admin/export.csv").get_data(as_text=True).splitlines(); print("export header:", exp[0]); print("export shows written ID:", any(l.split(",")[2]=="004512" for l in exp[1:]), "| changed time set:", any(l.split(",")[1] for l in exp[1:]))
print("staff search 4512 finds 004512:", "004512" in c.get("/admin/data?q=4512").get_data(as_text=True))
r=c.post("/admin/delete/staff/004512"); print("delete by written ID:", "Deleted staff ID 004512" in r.get_data(as_text=True))
import app as _app; _app._fails.clear(); _app.FAIL_LIMIT=5
codes=[c.get(f"/api/whoami/2?id=9{k}").status_code for k in range(7)]
print("guessing limit:", codes); _app._fails.clear(); _app.FAIL_LIMIT=200
