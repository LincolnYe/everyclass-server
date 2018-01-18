from .cal import cal_blueprint
from .commons import NoStudentException, NoClassException
from .config import load_config
from .query import query_blueprint

from flask import Flask, g, render_template, send_from_directory, redirect, url_for, flash
from flask_cdn import CDN
from htmlmin import minify
from termcolor import cprint
from markupsafe import escape
from raven.contrib.flask import Sentry


def create_app():
    app = Flask(__name__, static_folder='static', static_url_path='')
    app.config.from_object(load_config())
    cprint('App created. Running under [{}] config'.format(app.config['CONFIG_NAME']),color='blue')

    # CDN
    CDN(app)

    # Sentry
    sentry = Sentry(app)

    # register blueprints
    app.register_blueprint(cal_blueprint)
    app.register_blueprint(query_blueprint)

    # 结束时关闭数据库连接
    @app.teardown_appcontext
    def close_db(error):
        if hasattr(g, 'mysql_db'):
            g.mysql_db.close()

    # 首页
    @app.route('/')
    def main():
        return render_template('index.html')

    # 帮助
    @app.route('/faq')
    def faq():
        return render_template('faq.html')

    # 关于
    @app.route('/about')
    def about():
        return render_template('about.html')

    # 帮助
    @app.route('/guide')
    def guide():
        return render_template('guide.html')

    # 测试
    @app.route('/testing')
    def testing():
        return render_template('testing.html')

    @app.route('/<student_id>-<semester>.ics')
    def get_ics(student_id, semester):
        return send_from_directory("ics", student_id + "-" + semester + ".ics", as_attachment=True,
                                   mimetype='text/calendar')

    # Minify html response to decrease site traffic using htmlmin
    @app.after_request
    def response_minify(response):
        if app.config['HTML_MINIFY'] and response.content_type == u'text/html; charset=utf-8':
            response.set_data(
                minify(response.get_data(as_text=True))
            )
            return response
        return response

    # If STATIC_VERSIONED, use versioned file like 'style_v1_c012dr.css' instead of 'style_v1.css'
    @app.template_filter('versioned')
    def version_filter(filename):
        if app.config['STATIC_VERSIONED']:
            new_filename = app.config['STATIC_MANIFEST'][filename]
            return new_filename
        return filename

    # 404跳转回首页
    @app.errorhandler(404)
    def page_not_found(error):
        return redirect(url_for('main'))

    # 405跳转回首页
    @app.errorhandler(405)
    def method_not_allowed(error):
        return redirect(url_for('main'))

    @app.errorhandler(NoStudentException)
    def invalid_usage(error):
        flash('没有在数据库中找到你哦。是不是输错了？你刚刚输入的是%s' % escape(error))
        return redirect(url_for('main'))

    @app.errorhandler(NoClassException)
    def invalid_usage(error):
        flash('没有这门课程哦')
        return redirect(url_for('main'))

    @app.errorhandler(500)
    def internal_server_error(error):
        return render_template('500.html',
                               event_id=g.sentry_event_id,
                               public_dsn=sentry.client.get_public_dsn('https'))

    return app
