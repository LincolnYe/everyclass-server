"""
查询相关函数
"""
from typing import Dict, List, Tuple

import elasticapm
from flask import Blueprint, current_app as app, escape, flash, redirect, render_template, request, session, url_for

from everyclass.server.models import StudentSession
from everyclass.server.rpc import handle_exception_with_error_page
from everyclass.server.utils import contains_chinese
from everyclass.server.utils.decorators import disallow_in_maintenance, url_semester_check

query_blueprint = Blueprint('query', __name__)


@query_blueprint.route('/query', methods=['GET', 'POST'])
@disallow_in_maintenance
def query():
    """
    All in one 搜索入口，可以查询学生、老师、教室，然后跳转到具体资源页面

    正常情况应该是 post 方法，但是也兼容 get 防止意外情况，提高用户体验

    埋点：
    - `query_resource_type`, 查询的资源类型: classroom, single_student, single_teacher, multiple_people, or not_exist.
    - `query_type`, 查询方式（姓名、学工号）: by_name, by_id, other
    """
    import re
    from everyclass.server.rpc.api_server import APIServer

    # if under maintenance, return to maintenance.html
    if app.config["MAINTENANCE"]:
        return render_template("maintenance.html")

    # transform upper case xh to lower case(currently api-server does not support upper case xh)
    keyword = request.values.get('id')

    if not keyword:
        flash('请输入需要查询的姓名、学号、教工号或教室名称')
        return redirect(url_for('main.main'))

    if re.match('^[A-Za-z0-9]*$', request.values.get('id')):  # 学号工号转小写
        keyword = keyword.lower()

    # 调用 api-server 搜索
    with elasticapm.capture_span('rpc_search'):
        try:
            rpc_result = APIServer.search(keyword)
        except Exception as e:
            return handle_exception_with_error_page(e)

    # render different template for different resource types
    if len(rpc_result.classrooms) >= 1:  # 优先展示教室
        # we will use service name to filter apm document first, so it's not required to add service name prefix here
        elasticapm.tag(query_resource_type='classroom')
        elasticapm.tag(query_type='by_name')

        if len(rpc_result.classrooms) > 1:  # 多个教室选择
            return render_template('query/multipleClassroomChoice.html',
                                   name=keyword,
                                   classrooms=rpc_result.classrooms)
        return redirect('/classroom/{}/{}'.format(rpc_result.classrooms[0].room_id_encoded,
                                                  rpc_result.classrooms[0].semesters[-1]))
    elif len(rpc_result.students) == 1 and len(rpc_result.teachers) == 0:  # 一个学生
        elasticapm.tag(query_resource_type='single_student')
        if contains_chinese(keyword):
            elasticapm.tag(query_type='by_name')
        else:
            elasticapm.tag(query_type='by_id')

        if len(rpc_result.students[0].semesters) < 1:
            flash('没有可用学期')
            return redirect(url_for('main.main'))

        return redirect('/student/{}/{}'.format(rpc_result.students[0].student_id_encoded,
                                                rpc_result.students[0].semesters[-1]))
    elif len(rpc_result.teachers) == 1 and len(rpc_result.students) == 0:  # 一个老师
        elasticapm.tag(query_resource_type='single_teacher')
        if contains_chinese(keyword):
            elasticapm.tag(query_type='by_name')
        else:
            elasticapm.tag(query_type='by_id')

        if len(rpc_result.teachers[0].semesters) < 1:
            flash('没有可用学期')
            return redirect(url_for('main.main'))

        return redirect('/teacher/{}/{}'.format(rpc_result.teachers[0].teacher_id_encoded,
                                                rpc_result.teachers[0].semesters[-1]))
    elif len(rpc_result.teachers) >= 1 or len(rpc_result.students) >= 1:
        # multiple students, multiple teachers, or mix of both
        elasticapm.tag(query_resource_type='multiple_people')
        if contains_chinese(keyword):
            elasticapm.tag(query_type='by_name')
        else:
            elasticapm.tag(query_type='by_id')

        return render_template('query/peopleWithSameName.html',
                               name=keyword,
                               students=rpc_result.students,
                               teachers=rpc_result.teachers)
    else:
        elasticapm.tag(query_resource_type='not_exist')
        elasticapm.tag(query_type='other')
        flash('没有找到任何有关 {} 的信息，如果你认为这不应该发生，请联系我们。'.format(escape(request.values.get('id'))))
        return redirect(url_for('main.main'))


@query_blueprint.route('/student/<string:url_sid>/<string:url_semester>')
@url_semester_check
@disallow_in_maintenance
def get_student(url_sid: str, url_semester: str):
    """学生查询"""
    from everyclass.server.db.dao import PrivacySettingsDAO, VisitorDAO, RedisDAO
    from everyclass.server.utils import lesson_string_to_tuple
    from everyclass.server.rpc.api_server import APIServer
    from everyclass.server.utils.resource_identifier_encrypt import decrypt
    from everyclass.server.consts import MSG_INVALID_IDENTIFIER
    from everyclass.server.utils import semester_calculate
    from everyclass.server.consts import SESSION_LAST_VIEWED_STUDENT, SESSION_CURRENT_USER

    # decrypt identifier in URL
    try:
        _, student_id = decrypt(url_sid, resource_type='student')
    except ValueError:
        return render_template("common/error.html", message=MSG_INVALID_IDENTIFIER)

    # RPC to get student timetable
    with elasticapm.capture_span('rpc_get_student_timetable'):
        try:
            student = APIServer.get_student_timetable(student_id, url_semester)
        except Exception as e:
            return handle_exception_with_error_page(e)

    # save sid_orig to session for verifying purpose
    # must be placed before privacy level check. Otherwise a registered user could be redirected to register page.
    session[SESSION_LAST_VIEWED_STUDENT] = StudentSession(sid_orig=student.student_id,
                                                          sid=student.student_id_encoded,
                                                          name=student.name)

    # get privacy level, if current user has no permission to view, return now
    with elasticapm.capture_span('get_privacy_settings'):
        privacy_level = PrivacySettingsDAO.get_level(student.student_id)

    # 仅自己可见、且未登录或登录用户非在查看的用户，拒绝访问
    if privacy_level == 2 and (not session.get(SESSION_CURRENT_USER, None) or
                               session[SESSION_CURRENT_USER].sid_orig != student.student_id):
        return render_template('query/studentBlocked.html',
                               name=student.name,
                               falculty=student.deputy,
                               class_name=student.klass,
                               level=2)
    # 实名互访
    if privacy_level == 1:
        # 未登录，要求登录
        if not session.get(SESSION_CURRENT_USER, None):
            return render_template('query/studentBlocked.html',
                                   name=student.name,
                                   falculty=student.deputy,
                                   class_name=student.klass,
                                   level=1)
        # 仅自己可见的用户访问实名互访的用户，拒绝，要求调整自己的权限
        if PrivacySettingsDAO.get_level(session[SESSION_CURRENT_USER].sid_orig) == 2:
            return render_template('query/studentBlocked.html',
                                   name=student.name,
                                   falculty=student.deputy,
                                   class_name=student.klass,
                                   level=3)

    with elasticapm.capture_span('process_rpc_result'):
        courses: Dict[Tuple[int, int], List[Dict[str, str]]] = dict()
        for course in student.courses:
            day, time = lesson_string_to_tuple(course.lesson)
            if (day, time) not in courses:
                courses[(day, time)] = list()
            courses[(day, time)].append(course)
        empty_5, empty_6, empty_sat, empty_sun = _empty_column_check(courses)
        available_semesters = semester_calculate(url_semester, sorted(student.semesters))

    # 公开模式或实名互访模式，留下轨迹
    if privacy_level != 2 and \
            session.get(SESSION_CURRENT_USER, None) and \
            session[SESSION_CURRENT_USER] != session[SESSION_LAST_VIEWED_STUDENT]:
        VisitorDAO.update_track(host=student.student_id,
                                visitor=session[SESSION_CURRENT_USER])

    # 增加访客记录
    RedisDAO.add_visitor_count(student.student_id, session.get(SESSION_CURRENT_USER, None))

    return render_template('query/student.html',
                           student=student,
                           classes=courses,
                           empty_sat=empty_sat,
                           empty_sun=empty_sun,
                           empty_6=empty_6,
                           empty_5=empty_5,
                           available_semesters=available_semesters,
                           current_semester=url_semester)


@query_blueprint.route('/teacher/<string:url_tid>/<string:url_semester>')
@disallow_in_maintenance
@url_semester_check
def get_teacher(url_tid, url_semester):
    """老师查询"""
    from collections import defaultdict

    from everyclass.server.utils import lesson_string_to_tuple
    from everyclass.server.utils import semester_calculate
    from everyclass.server.rpc.api_server import APIServer
    from everyclass.server.utils.resource_identifier_encrypt import decrypt
    from everyclass.server.consts import MSG_INVALID_IDENTIFIER

    # decrypt identifier in URL
    try:
        _, teacher_id = decrypt(url_tid, resource_type='teacher')
    except ValueError:
        return render_template("common/error.html", message=MSG_INVALID_IDENTIFIER)

    # RPC to get teacher timetable
    with elasticapm.capture_span('rpc_get_teacher_timetable'):
        try:
            teacher = APIServer.get_teacher_timetable(teacher_id, url_semester)
        except Exception as e:
            return handle_exception_with_error_page(e)

    with elasticapm.capture_span('process_rpc_result'):
        courses = defaultdict(list)
        for course in teacher.courses:
            day, time = lesson_string_to_tuple(course.lesson)
            if (day, time) not in courses:
                courses[(day, time)] = list()
            courses[(day, time)].append(course)

    empty_5, empty_6, empty_sat, empty_sun = _empty_column_check(courses)

    available_semesters = semester_calculate(url_semester, teacher.semesters)

    return render_template('query/teacher.html',
                           teacher=teacher,
                           classes=courses,
                           empty_sat=empty_sat,
                           empty_sun=empty_sun,
                           empty_6=empty_6,
                           empty_5=empty_5,
                           available_semesters=available_semesters,
                           current_semester=url_semester)


@query_blueprint.route('/classroom/<string:url_rid>/<string:url_semester>')
@url_semester_check
@disallow_in_maintenance
def get_classroom(url_rid, url_semester):
    """教室查询"""
    from collections import defaultdict

    from everyclass.server.utils import lesson_string_to_tuple
    from everyclass.server.utils import semester_calculate
    from everyclass.server.rpc.api_server import APIServer
    from everyclass.server.utils.resource_identifier_encrypt import decrypt
    from everyclass.server.consts import MSG_INVALID_IDENTIFIER

    # decrypt identifier in URL
    try:
        _, room_id = decrypt(url_rid, resource_type='room')
    except ValueError:
        return render_template("common/error.html", message=MSG_INVALID_IDENTIFIER)

    # RPC to get classroom timetable
    with elasticapm.capture_span('rpc_get_classroom_timetable'):
        try:
            room = APIServer.get_classroom_timetable(url_semester, room_id)
        except Exception as e:
            return handle_exception_with_error_page(e)

    with elasticapm.capture_span('process_rpc_result'):
        courses = defaultdict(list)
        for course in room.courses:
            day, time = lesson_string_to_tuple(course.lesson)
            courses[(day, time)].append(course)

    empty_5, empty_6, empty_sat, empty_sun = _empty_column_check(courses)

    available_semesters = semester_calculate(url_semester, room.semesters)

    return render_template('query/room.html',
                           room=room,
                           rid=url_rid,
                           classes=courses,
                           empty_sat=empty_sat,
                           empty_sun=empty_sun,
                           empty_6=empty_6,
                           empty_5=empty_5,
                           available_semesters=available_semesters,
                           current_semester=url_semester)


@query_blueprint.route('/course/<string:url_cid>/<string:url_semester>')
@url_semester_check
@disallow_in_maintenance
def get_course(url_cid: str, url_semester: str):
    """课程查询"""
    from everyclass.server.utils import lesson_string_to_tuple
    from everyclass.server.utils import get_time_chinese
    from everyclass.server.utils import get_day_chinese
    from everyclass.server.utils.resource_identifier_encrypt import decrypt
    from everyclass.server.rpc.api_server import APIServer, teacher_list_to_str
    from everyclass.server.consts import MSG_INVALID_IDENTIFIER

    # decrypt identifier in URL
    try:
        _, course_id = decrypt(url_cid, resource_type='klass')
    except ValueError:
        return render_template("common/error.html", message=MSG_INVALID_IDENTIFIER)

    # RPC to get course
    with elasticapm.capture_span('rpc_get_course'):
        try:
            course = APIServer.get_course(url_semester, course_id)
        except Exception as e:
            return handle_exception_with_error_page(e)

    day, time = lesson_string_to_tuple(course.lesson)

    # 给“文化素质类”等加上“课”后缀
    if course.type and course.type[-1] != '课':
        course.type = course.type + '课'

    return render_template('query/course.html',
                           course=course,
                           course_day=get_day_chinese(day),
                           course_time=get_time_chinese(time),
                           show_union_class=not course.union_name.isdigit(),  # 合班名称为数字时不展示合班名称
                           course_teacher=teacher_list_to_str(course.teachers),
                           current_semester=url_semester
                           )


def _empty_column_check(courses: dict) -> Tuple[bool, bool, bool, bool]:
    """检查是否周末和晚上有课，返回三个布尔值"""
    with elasticapm.capture_span('_empty_column_check'):
        # 空闲周末判断，考虑到大多数人周末都是没有课程的
        empty_sat = True
        for cls_time in range(1, 7):
            if (6, cls_time) in courses:
                empty_sat = False

        empty_sun = True
        for cls_time in range(1, 7):
            if (7, cls_time) in courses:
                empty_sun = False

        # 空闲课程判断，考虑到大多数人11-12节都是没有课程的
        empty_6 = True
        for cls_day in range(1, 8):
            if (cls_day, 6) in courses:
                empty_6 = False
        empty_5 = True
        for cls_day in range(1, 8):
            if (cls_day, 5) in courses:
                empty_5 = False
    return empty_5, empty_6, empty_sat, empty_sun
