# SETUP — VS Code mein kaise chalu karein

Ek baar ka kaam. 15–20 minute.

---

## Pehle ek confusion door kar lo

Do alag cheezein hain, log yahan atakte hain:

| | Kya hai | Paisa |
|---|---|---|
| **VS Code ka Claude Code** | Tumhara coding assistant | Tumhare Claude subscription se chalta hai. API key nahi chahiye. |
| **`extract.py` ki API key** | Tumhare *product* ka dimaag — bill padhega | console.anthropic.com se alag key. Credits daalne padenge. |

Dono mix mat karo.

---

## Step 1 — Folder banao

Desktop ya Documents mein ek folder banao: **`bill-checker`**

Saari files usi ke andar daalo. Koi sub-folder nahi:

```
bill-checker/
├── CLAUDE.md
├── SETUP.md
├── README.md
├── requirements.txt
├── main.py
├── extract.py
├── checker.py
├── db.py
├── make_sample_invoice.py
└── sample_invoice.jpg
```

VS Code kholo → **File > Open Folder** → `bill-checker` chuno.

> Ek file nahi, **pura folder** kholna hai. Warna Claude Code baaki files
> dekh nahi payega aur `CLAUDE.md` bhi nahi padhega.

---

## Step 2 — Setup

VS Code mein **Terminal > New Terminal**. Ek-ek command chalao:

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Dusri command ke baad terminal mein `(venv)` dikhne lagega — matlab sahi hai.

> Har naye terminal mein `source venv/bin/activate` dobara chalana padega.
> Bhool gaye to "module not found" error aayega.

---

## Step 3 — Mock mode mein test karo

```bash
python main.py sample_invoice.jpg --mock --received "1:94, 2:48"
```

Ye aana chahiye:

```
[!] MAGGI NOODLES 70g (12pk)   ₹100
[!] TATA SALT 1KG              ₹64
[!] PARLE-G BISCUIT 100g       ₹60
TOTAL PHANSA PAISA: Rs224.00
```

**₹224 aa gaya? Matching engine chal raha hai.** Isme koi AI call nahi hui —
bas dummy data se logic test hua. Ye number yaad rakho, aage har badlav ke
baad isse check karna hai.

---

## Step 4 — API key lo, asli AI se test karo

1. console.anthropic.com par account banao
2. **Billing** → thode credits daalo ($5 shuruat ke liye kaafi hai)
3. **API Keys** → nayi key banao, copy karo

```bash
export ANTHROPIC_API_KEY=sk-ant-...
python extract.py --list-models
```

Model names ki list aa gayi? Key kaam kar rahi hai. Ab:

```bash
python main.py sample_invoice.jpg
```

Is baar asli AI photo padhega. **Jo JSON nikle usse sample bill se aankhon se
compare karo** — 8 items sahi? qty sahi? rate sahi? Yahi asli test hai.

Tedhi photo par bhi dekho:

```bash
python make_sample_invoice.py --messy --out messy.jpg
python extract.py messy.jpg
```

---

## Step 5 — Git chalu karo (skip mat karna)

```bash
git init
printf 'venv/\n*.db\n__pycache__/\n.env\n' > .gitignore
git add .
git commit -m "working prototype"
```

Kal tum AI se 10 badlav karwaoge — 7 achhe honge, 3 kharab. Git ke bina wapas
nahi ja paoge. Aur `.gitignore` zaroori hai warna API key ya database galti se
upload ho jayega.

Jab bhi kuch kaam karne lage, turant:

```bash
git add . && git commit -m "kya kiya"
```

---

## Step 6 — Claude Code kholo

Left sidebar mein **Spark icon** dabao.

Pehla message ye do — code likhwane ke liye nahi, ye check karne ke liye ki
usko project dikh raha hai:

```
Read CLAUDE.md and all the Python files, then explain in Hinglish
how data flows from photo to final report.
```

Agar wo files ke naam aur logic sahi bata de — tum ready ho.

---

## Ab kaam kaise karna hai

**Chhoti, saaf request do.** Achhe examples:

```
In @checker.py, add a rule that flags the same item appearing
twice in one invoice. Follow the pattern of check_line_math.
```

```
@extract.py ka SYSTEM_PROMPT handwritten invoices ke liye
improve karo.
```

Bure examples: *"pura product bana do"*, *"WhatsApp bot jod do"*.

Kaam ke tareeke:

- File ka naam **`@`** se mention karo (`@checker.py`) — Claude seedha wahi file dekhega
- Bade badlav se pehle **Shift+Tab do baar** — plan mode on hoga, plan padh ke approve karo
- Har badlav ke baad Step 3 ka `--mock` test dobara chalao. ₹224 aana chahiye.
- Kaam karne laga? Turant commit.

---

## Sabse badi galti jo log karte hain

Claude Code se "sab kuch ek saath bana do" kehna.

Wo bana bhi dega, chalega bhi. Par tum samajh nahi paoge ki andar kya hai —
aur kal jab kisi dukaan par asli bill par toota, fix nahi kar paoge.

**Ek waqt par ek cheez. Samajh kar. Commit karke.**

---

## Ab asli kaam

Code ho gaya. Ab jo chahiye wo computer par nahi milega:

**20 asli supplier bills.** Printed, thermal, handwritten, mude-tude, dhundhli
photo — sab tarah ke. Dukaandaron se maango.

Unpar chala kar dekho kitne bilkul sahi nikle. 85%+ chahiye. Us number se
neeche ho, to koi bhi naya feature bekaar hai.
