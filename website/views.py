from flask import (
    Blueprint,
    render_template,
    current_app,
    request,
    redirect,
    url_for,
    flash,
    abort,
    send_from_directory,
)
from flask_login import login_required, current_user
import json
import os
import csv
from datetime import datetime
from werkzeug.utils import secure_filename
import re

views = Blueprint('views', __name__)

ALLOWED_EXTENSIONS = {'json'}


@views.route('/')
@login_required
def home():
    return render_template("home.html", user=current_user)


# ---------- OUTILS ----------

def questionnaires_dir():
    return os.path.join(current_app.root_path, 'questionnaire')


def results_dir():
    return os.path.join(current_app.root_path, 'results')


def list_questionnaires():
    q_dir = questionnaires_dir()
    questionnaires = []

    if os.path.isdir(q_dir):
        for fname in os.listdir(q_dir):
            if fname.endswith('.json'):
                qid = os.path.splitext(fname)[0]
                csv_path = os.path.join(results_dir(), f'{qid}.csv')
                has_results = os.path.exists(csv_path)
                questionnaires.append({
                    'id': qid,
                    'filename': fname,
                    'has_results': has_results,
                })

    questionnaires.sort(key=lambda x: x["id"])
    return questionnaires


def slugify(text: str) -> str:
    text = (text or "").strip().lower()
    text = re.sub(r"[^a-z0-9_]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("_")
    return text or "question"


def normalize_questions(raw):
    """
    Renvoie une liste standard:
      {id,key,label,description,type,required,options?}

    Types supportés:
      - text, number, date
      - choice  (choix unique)
      - multi   (choix multiple)
    """
    if raw is None:
        return []

    if isinstance(raw, dict) and "questions" in raw:
        raw = raw["questions"]

    if not isinstance(raw, list):
        return []

    normalized = []
    used_keys = set()

    for i, q in enumerate(raw, start=1):
        if not isinstance(q, dict):
            continue

        # Nouveau format
        if "key" in q and ("label" in q or "question" in q):
            key = str(q.get("key") or "").strip()
            label = str(q.get("label") or q.get("question") or "").strip()
            desc = str(q.get("description") or q.get("desc") or "").strip()
            qtype = str(q.get("type") or "text").strip()
            required = bool(q.get("required", False))
            options = q.get("options")
        else:
            # Ancien format
            label = str(q.get("question") or q.get("label") or "").strip()
            desc = str(q.get("description") or "").strip()
            options = q.get("options")
            qtype = "choice" if isinstance(options, list) and len(options) > 0 else "text"
            required = bool(q.get("required", True))

            key = str(q.get("key") or "").strip()
            if not key:
                key = slugify(label) or f"q{i}"

        if not label:
            continue

        if not key:
            key = f"q{i}"
        key = slugify(key)

        # éviter doublons de key
        base_key = key
        n = 2
        while key in used_keys:
            key = f"{base_key}_{n}"
            n += 1
        used_keys.add(key)

        # options propres
        if qtype in ("choice", "multi"):
            if not isinstance(options, list):
                options = []
            options = [str(o).strip() for o in options if str(o).strip()]
        else:
            options = None

        out = {
            "id": i,
            "key": key,
            "label": label,
            "description": desc,
            "type": qtype,
            "required": required,
        }
        if options is not None:
            out["options"] = options

        normalized.append(out)

    return normalized


def load_questions(qid: str):
    filename = os.path.join(questionnaires_dir(), f'{qid}.json')
    try:
        with open(filename, 'r', encoding='utf-8') as file:
            raw = json.load(file)
            return normalize_questions(raw)
    except FileNotFoundError:
        return None
    except json.JSONDecodeError:
        return []


def save_answers(qid: str, questions, answers: dict):
    r_dir = results_dir()
    os.makedirs(r_dir, exist_ok=True)

    csv_path = os.path.join(r_dir, f'{qid}.csv')

    fieldnames = ['date', 'user_id', 'user_name', 'user_email'] + [
        q['key'] for q in questions
    ]

    file_exists = os.path.exists(csv_path)

    with open(csv_path, 'a', newline='', encoding='utf-8') as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames, delimiter=';')

        if not file_exists:
            writer.writeheader()

        row = {
            'date': datetime.now().strftime("%d/%m/%Y"),
            'user_id': current_user.id,
            'user_name': current_user.first_name,
            'user_email': current_user.email,
        }

        for q in questions:
            k = q["key"]
            row[k] = answers.get(k, "")

        writer.writerow(row)


def allowed_file(filename: str) -> bool:
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


# liste des questionnaire (utilisateur)

@views.route('/quiz')
@login_required
def quiz():
    questionnaires = list_questionnaires()

    if not questionnaires:
        flash("Aucun questionnaire disponible pour le moment.", "info")
        return redirect(url_for("views.home"))

    return render_template(
        "quiz_list.html",
        user=current_user,
        questionnaires=questionnaires
    )


# questionnaire (utilisateur)

@views.route('/q/<qid>', methods=['GET', 'POST'])
@login_required
def questionnaire(qid):
    questions = load_questions(qid)

    if questions is None:
        flash(f"Questionnaire '{qid}' introuvable.", "error")
        return redirect(url_for('views.quiz'))

    if not questions:
        flash("Le fichier JSON est vide ou n'a pas un format lisible.", "error")
        return redirect(url_for('views.quiz'))

    if request.method == 'POST':
        answers = {}
        errors = []

        for q in questions:
            key = q["key"]
            label = q.get("label", key)
            qtype = q.get("type", "text")
            required = bool(q.get("required", False))

            # lecture de lavaleur
            if qtype == "multi":
                selected = request.form.getlist(f"{key}[]")
                value = " | ".join([v.strip() for v in selected if v.strip()]).strip()
            else:
                value = (request.form.get(key, "") or "").strip()

            # réponse obligatoire
            if required and not value:
                errors.append(f"'{label}' est obligatoire.")
                continue

            # cas reponse vide et non requis
            if not value:
                answers[key] = ""
                continue

            # réponse type valide
            if qtype == "number":
                try:
                    float(value.replace(",", "."))
                except ValueError:
                    errors.append(f"'{label}' doit être un nombre valide.")
                    continue

            elif qtype == "date":
                try:
                    datetime.strptime(value, "%Y-%m-%d")
                except ValueError:
                    errors.append(f"'{label}' doit être une date valide (YYYY-MM-DD).")
                    continue

            elif qtype == "choice":
                opts = q.get("options") or []
                if opts and value not in opts:
                    errors.append(f"Réponse invalide pour '{label}'.")
                    continue

            elif qtype == "multi":
                opts = q.get("options") or []
                if opts:
                    chosen = [v.strip() for v in value.split("|") if v.strip()]
                    invalid = [v for v in chosen if v not in opts]
                    if invalid:
                        errors.append(f"Réponses invalides pour '{label}'.")
                        continue

            answers[key] = value

        if errors:
            for msg in errors:
                flash(msg, "error")
            return render_template(
                "quiz.html",
                user=current_user,
                questions=questions,
                qid=qid
            )

        save_answers(qid, questions, answers)
        flash("Merci ! Vos réponses ont été enregistrées.", "success")
        return redirect(url_for('views.quiz'))

    return render_template(
        "quiz.html",
        user=current_user,
        questions=questions,
        qid=qid
    )


# Liste des questionnaire (admin)

@views.route('/admin/questionnaires')
@login_required
def admin_questionnaires():
    if not current_user.is_admin:
        abort(403)

    questionnaires = list_questionnaires()
    return render_template(
        "admin_questionnaires.html",
        user=current_user,
        questionnaires=questionnaires
    )


# Supprimée les questionnaire (admin)

@views.route('/admin/questionnaires/<qid>/delete', methods=['POST'])
@login_required
def admin_delete_questionnaire(qid):
    if not current_user.is_admin:
        abort(403)

    q_json = os.path.join(questionnaires_dir(), f"{qid}.json")
    q_csv = os.path.join(results_dir(), f"{qid}.csv")

    deleted = False

    if os.path.exists(q_json):
        os.remove(q_json)
        deleted = True

    if os.path.exists(q_csv):
        os.remove(q_csv)

    if deleted:
        flash(f"Questionnaire '{qid}' supprimé avec succès.", "success")
    else:
        flash(f"Questionnaire '{qid}' introuvable.", "error")

    return redirect(url_for('views.admin_questionnaires'))


# Creation questionnaire

@views.route('/admin/questionnaires/create', methods=['GET', 'POST'])
@login_required
def admin_create_questionnaire():
    if not current_user.is_admin:
        abort(403)

    if request.method == 'POST':
        qid_raw = request.form.get('qid', '')
        qid = slugify(qid_raw)

        if not qid:
            flash("Identifiant invalide.", "error")
            return redirect(request.url)

        q_dir = questionnaires_dir()
        os.makedirs(q_dir, exist_ok=True)

        json_path = os.path.join(q_dir, f"{qid}.json")
        if os.path.exists(json_path):
            flash("Un questionnaire avec cet identifiant existe déjà.", "error")
            return redirect(request.url)

        questions = []
        idx = 1
        while True:
            label = (request.form.get(f"label_{idx}") or "").strip()
            key = (request.form.get(f"key_{idx}") or "").strip()
            desc = (request.form.get(f"desc_{idx}") or "").strip()
            qtype = (request.form.get(f"type_{idx}") or "text").strip()
            required = bool(request.form.get(f"required_{idx}"))

            if not label and not key and not desc and idx > 1:
                break

            if not label:
                idx += 1
                continue

            if not key:
                key = slugify(label)
            key = slugify(key)

            q = {
                "key": key,
                "label": label,
                "description": desc,
                "type": qtype,
                "required": required,
            }

            if qtype in ("choice", "multi"):
                options = []
                opt_i = 1
                while True:
                    opt = request.form.get(f"option_{idx}_{opt_i}")
                    if opt is None:
                        break
                    opt = opt.strip()
                    if opt:
                        options.append(opt)
                    opt_i += 1
                q["options"] = options

            questions.append(q)
            idx += 1

        if not questions:
            flash("Ajoute au moins une question.", "error")
            return redirect(request.url)

        normalized = normalize_questions({"questions": questions})
        if not normalized:
            flash("Impossible de créer le questionnaire (questions invalides).", "error")
            return redirect(request.url)

        payload = {"questions": normalized}
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

        flash(f"Questionnaire '{qid}' créé.", "success")
        return redirect(url_for('views.admin_questionnaires'))

    return render_template("admin_create.html", user=current_user)


# Afficher resultat (admin)

@views.route('/admin/questionnaires/<qid>/results')
@login_required
def admin_questionnaire_results(qid):
    if not current_user.is_admin:
        abort(403)

    r_dir = results_dir()
    csv_path = os.path.join(r_dir, f'{qid}.csv')

    rows = []
    fieldnames = []

    if os.path.exists(csv_path):
        with open(csv_path, 'r', encoding='utf-8') as csvfile:
            reader = csv.DictReader(csvfile, delimiter=';')
            fieldnames = reader.fieldnames or []
            for row in reader:
                rows.append(row)
    else:
        flash("Aucun résultat trouvé pour ce questionnaire.", "info")

    return render_template(
        "admin_results.html",
        user=current_user,
        qid=qid,
        fieldnames=fieldnames,
        rows=rows
    )


@views.route('/admin/questionnaires/<qid>/results/download')
@login_required
def admin_download_results(qid):
    if not current_user.is_admin:
        abort(403)

    r_dir = results_dir()
    csv_filename = f"{qid}.csv"
    csv_path = os.path.join(r_dir, csv_filename)

    if not os.path.exists(csv_path):
        flash("Aucun fichier de résultats à télécharger pour ce questionnaire.", "info")
        return redirect(url_for('views.admin_questionnaire_results', qid=qid))

    return send_from_directory(
        r_dir,
        csv_filename,
        as_attachment=True,
        download_name=f"{qid}_results.csv"
    )


# importer un questionnaire json

@views.route('/admin/questionnaires/upload', methods=['GET', 'POST'])
@login_required
def admin_upload_questionnaire():
    if not current_user.is_admin:
        abort(403)

    if request.method == 'POST':
        file = request.files.get('file')

        if not file or file.filename == '':
            flash("Aucun fichier sélectionné.", "error")
            return redirect(request.url)

        if not allowed_file(file.filename):
            flash("Seuls les fichiers .json sont autorisés.", "error")
            return redirect(request.url)

        filename = secure_filename(file.filename)
        q_dir = questionnaires_dir()
        os.makedirs(q_dir, exist_ok=True)
        save_path = os.path.join(q_dir, filename)
        file.save(save_path)

        qid = os.path.splitext(filename)[0]
        test = load_questions(qid)
        if not test:
            flash("Import OK, mais le JSON n'est pas reconnu (format inattendu ou vide).", "error")

        flash(f"Questionnaire '{filename}' importé.", "success")
        return redirect(url_for('views.admin_questionnaires'))

    return render_template("admin_upload.html", user=current_user)
