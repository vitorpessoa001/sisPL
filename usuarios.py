# usuarios.py
import sqlite3
from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_user, logout_user, login_required, current_user, UserMixin
from flask_bcrypt import Bcrypt

usuarios_bp = Blueprint('usuarios', __name__, template_folder='templates')
bcrypt = Bcrypt()

# ----------------------------------------------------------
# Classe e utilitários de banco
# ----------------------------------------------------------
class Usuario(UserMixin):
    def __init__(self, id, username, password, role):
        self.id = id
        self.username = username
        self.password = password
        self.role = role

def get_db():
    return sqlite3.connect('users.db')

def buscar_usuario_por_id(user_id):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT id, username, password, role FROM users WHERE id = ?", (user_id,))
    u = c.fetchone()
    conn.close()
    if u:
        return Usuario(*u)
    return None

def buscar_usuario_por_nome(username):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT id, username, password, role FROM users WHERE username = ?", (username,))
    u = c.fetchone()
    conn.close()
    if u:
        return Usuario(*u)
    return None

# ----------------------------------------------------------
# LOGIN / LOGOUT
# ----------------------------------------------------------
@usuarios_bp.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('selecionar_data'))

    if request.method == 'POST':
        username = request.form['username']
        senha = request.form['password']
        usuario = buscar_usuario_por_nome(username)
        if usuario and bcrypt.check_password_hash(usuario.password, senha):
            login_user(usuario)
            flash('Login realizado com sucesso.', 'success')
            return redirect(url_for('selecionar_data'))
        else:
            flash('Usuário ou senha incorretos.', 'danger')
    return render_template('login.html')

@usuarios_bp.route('/logout')
@login_required
def logout():
    logout_user()
    flash('Sessão encerrada.', 'info')
    return redirect(url_for('usuarios.login'))

# ----------------------------------------------------------
# ADMINISTRAÇÃO DE USUÁRIOS (somente Admin)
# ----------------------------------------------------------
@usuarios_bp.route('/admin/usuarios')
@login_required
def admin_usuarios():
    if current_user.role != 'Admin':
        flash('Acesso restrito a administradores.', 'danger')
        return redirect(url_for('selecionar_data'))

    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT id, username, role FROM users ORDER BY id DESC")
    usuarios = [{'id': r[0], 'username': r[1], 'role': r[2]} for r in c.fetchall()]
    conn.close()
    return render_template('admin_usuarios.html', usuarios=usuarios)

@usuarios_bp.route('/admin/usuarios/criar', methods=['POST'])
@login_required
def admin_criar_usuario():
    if current_user.role != 'Admin':
        flash('Acesso restrito.', 'danger')
        return redirect(url_for('usuarios.admin_usuarios'))

    username = request.form['username']
    senha = request.form['password']
    role = request.form['role']
    senha_hash = bcrypt.generate_password_hash(senha).decode('utf-8')

    conn = get_db()
    c = conn.cursor()
    try:
        c.execute("INSERT INTO users (username, password, role) VALUES (?, ?, ?)", (username, senha_hash, role))
        conn.commit()
        flash('Usuário criado com sucesso.', 'success')
    except sqlite3.IntegrityError:
        flash('Usuário já existe.', 'danger')
    finally:
        conn.close()

    return redirect(url_for('usuarios.admin_usuarios'))

@usuarios_bp.route('/admin/usuarios/excluir/<int:id>')
@login_required
def admin_excluir_usuario(id):
    if current_user.role != 'Admin':
        flash('Acesso restrito.', 'danger')
        return redirect(url_for('usuarios.admin_usuarios'))

    if id == current_user.id:
        flash('Você não pode excluir a própria conta.', 'warning')
        return redirect(url_for('usuarios.admin_usuarios'))

    conn = get_db()
    c = conn.cursor()
    c.execute("DELETE FROM users WHERE id=?", (id,))
    conn.commit()
    conn.close()
    flash('Usuário excluído com sucesso.', 'success')
    return redirect(url_for('usuarios.admin_usuarios'))

@usuarios_bp.route('/admin/usuarios/editar', methods=['POST'])
@login_required
def admin_editar_usuario():
    if current_user.role != 'Admin':
        flash('Acesso restrito.', 'danger')
        return redirect(url_for('usuarios.admin_usuarios'))

    user_id = request.form['id']
    username = request.form['username']
    role = request.form['role']
    senha = request.form['password']

    conn = get_db()
    c = conn.cursor()

    if senha.strip():
        senha_hash = bcrypt.generate_password_hash(senha).decode('utf-8')
        c.execute("UPDATE users SET username=?, password=?, role=? WHERE id=?", (username, senha_hash, role, user_id))
    else:
        c.execute("UPDATE users SET username=?, role=? WHERE id=?", (username, role, user_id))

    conn.commit()
    conn.close()
    flash('Usuário atualizado com sucesso.', 'success')
    return redirect(url_for('usuarios.admin_usuarios'))

