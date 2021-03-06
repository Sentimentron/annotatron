import json
import logging
import random
import string
from datetime import datetime, timedelta
from wsgiref import simple_server

import bcrypt
import falcon
from Crypto.Cipher import ARC4
from pyannotatron.models import ConfigurationResponse, NewUserRequest, ValidationError, FieldError, LoginRequest, \
    LoginResponse, AnnotatronUser, UserKind, Corpus, BinaryAsset, BinaryAssetDescription, BinaryAssetKind, \
    AbstractQuestion, Question, SuccessfulInsert, Assignment, Annotation, AssignmentResponse
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models import InternalUser, InternalToken, InternalCorpus, InternalAsset, InternalQuestion,\
    InternalAssignment, InternalAssignmentAssetXRef, AssignmentAction, InternalAssignmentHistory

Session = sessionmaker()


def obfuscate_int64_field(x):
    cipher = ARC4.new("habppootle")
    cipher.encrypt(b'\xbe\x89\xd0\xb1 \xb8\x99\xbd')
    return int.from_bytes(cipher.encrypt((x).to_bytes(8, byteorder='big')), 'big')


def recover_int64_field(x):
    cipher = ARC4.new("habppootle")
    cipher.encrypt(b'\xbe\x89\xd0\xb1 \xb8\x99\xbd')
    return int.from_bytes(cipher.decrypt((int(x)).to_bytes(8, byteorder='big')), byteorder='big')


class AssignmentController:

    def __init__(self, storage):
        self.storage = storage

    def convert(self, a:InternalAssignment) -> Assignment:
        converted_assets = [x.asset_id for x in a.asset_refs]
        ret = Assignment(
            assets=converted_assets,
            assigned_annotator_id=a.annotator_id,
            assigned_user_id=a.assigned_user_id,
            question=Question.from_json(a.question),
            response=None,
            created=a.created,
            assigned_reviewer_id=a.reviewer_id
        )
        if a.response:
            ret.response = Annotation.from_json(a.response)
        return ret

    def create_assigment(self, new_assignment:Assignment, c:InternalCorpus) -> (SuccessfulInsert, ValidationError):
        """
        Checks an Assignment for issues, then logs it into the database.
        :param new_assignment:
        :return: (SuccessfulInsert, None) on Success.
        """

        new_assignment.assigned_annotator_id = recover_int64_field(new_assignment.assigned_annotator_id)
        if new_assignment.assigned_reviewer_id:
            new_assignment.assigned_reviewer_id = recover_int64_field(new_assignment.assigned_reviewer_id)
        new_assignment.assigned_user_id = recover_int64_field(new_assignment.assigned_user_id)

        for i, a in enumerate(new_assignment.assets):
            new_assignment.assets[i] = recover_int64_field(a)

        resolved_xrefs = []
        asset_controller = AssetController(self.storage)
        for id in new_assignment.assets:
            asset = asset_controller.get_asset_with_id(id)
            if asset is None:
                return None, ValidationError([FieldError("assets", "Could not resolve one or more Assets", False)])
            xref = InternalAssignmentAssetXRef(asset=asset)
            resolved_xrefs.append(xref)
            self.storage.add(xref)

        an = InternalAssignment(
            summary_code=new_assignment.question.summary_code,
            assigned_user_id=new_assignment.assigned_annotator_id,
            annotator_id=new_assignment.assigned_annotator_id,
            question=new_assignment.question.to_json(),
            response=None,
            reviewer_id=new_assignment.assigned_reviewer_id,
            created=datetime.utcnow(),
            corpus=c,
            state="created"
        )

        self.storage.begin_nested()
        self.storage.add(an)
        self.storage.commit()

        for x in resolved_xrefs:
            x.assignment = an
        self.storage.commit()
        return SuccessfulInsert(id=obfuscate_int64_field(an.id)), None

    def retrieve_assignment(self, non_obfuscated_id: int) -> InternalAssignment:
        return self.storage.query(InternalAssignment).get(non_obfuscated_id)

    def retrieve_assignments_for_corpus(self, corpus: InternalCorpus) -> [InternalAssignment]:
        return self.storage.query(InternalAssignment).filter_by(corpus_id=corpus.id)

    def retrieve_assignments_for_user(self, non_obfuscated_id: int) -> [InternalAssignment]:
        user_controller = UserController(self.storage)
        user = user_controller.get_user_from_id(non_obfuscated_id)
        return self.storage.query(InternalAssignment).filter_by(assigned_user=user)

    def update_assignment(self, non_obfuscated_id:int, user_provided_assignment:AssignmentResponse,
                          current_user: InternalUser, action: str) -> ValidationError:
        db_assignment = self.retrieve_assignment(non_obfuscated_id)
        user_provided_response_json = None
        if user_provided_assignment.response:
            user_provided_response_json = user_provided_assignment.response.to_json()

        if action == AssignmentAction.APPROVE:
            # If the reviewer approves the annotation, place into the "approved" state
            reviewer = db_assignment.assigned_reviewer
            if reviewer != current_user:
                return ValidationError([FieldError("_user", "Not responsible for approving this Annotation")])
            ah = InternalAssignmentHistory(assignment_id=db_assignment.id, state="Approved",
                                           notes=user_provided_assignment.notes,
                                           response=user_provided_response_json,
                                           updating_user_id=current_user.id)
            db_assignment.state = "approved"
            db_assignment.completed = datetime.utcnow()
            db_assignment.updated = datetime.utcnow()
            if user_provided_assignment.response:
                db_assignment.response = user_provided_assignment.response.to_json()
            db_assignment.assigned_user_id = None
            self.storage.add(ah)
            self.storage.commit()
        elif action == AssignmentAction.REJECT:
            # If rejected, assign back to the annotator.
            reviewer = db_assignment.assigned_reviewer
            if reviewer != current_user:
                return ValidationError([FieldError("_user", "Not responsible for approving this Annotation")])
            ah = InternalAssignmentHistory(assignment_id=db_assignment.id, state="Rejected",
                                           notes=user_provided_assignment.notes,
                                           response=user_provided_assignment.response,
                                           updating_user_id=current_user.id)
            db_assignment.state = "created"
            db_assignment.completed = None
            db_assignment.updated = datetime.utcnow()
            if user_provided_assignment.response:
                db_assignment.response = user_provided_assignment.response.to_json()
            db_assignment.assigned_user_id = db_assignment.annotator_id
            self.storage.add(ah)
            self.storage.commit()
        elif action == AssignmentAction.SUBMIT_FOR_REVIEW:
            annotator = db_assignment.assigned_user
            if current_user != annotator:
                if annotator != current_user:
                    return ValidationError([FieldError("_user", "Not responsible for this Assignment")])
            ah = InternalAssignmentHistory(assignment_id=db_assignment.id, state="Submitted",
                                           notes=user_provided_assignment.notes,
                                           response=user_provided_response_json,
                                           updating_user_id=current_user.id)
            if not db_assignment.reviewer_id:
                # If there's no reviewer, then automatically place the annotation into the approved state.
                db_assignment.state = "approved"
                db_assignment.completed = datetime.utcnow()
                if user_provided_assignment.response:
                    db_assignment.response = user_provided_assignment.response.to_json()
                db_assignment.assigned_user_id = None
            else:
                # Otherwise, assign it to the reviewer
                db_assignment.state = "pending"
                db_assignment.completed = None
                db_assignment.updated = datetime.utcnow()
                if user_provided_assignment.response:
                    db_assignment.response = user_provided_response_json
                db_assignment.assigned_user_id = db_assignment.reviewer_id
            self.storage.add(ah)
            self.storage.commit()
        else:
            raise ValueError((action, "Shouldn't reach here"))

    def delete_assignment(self, non_obfuscated_id:int) -> ValidationError:
        db_assignment = self.retrieve_assignment(non_obfuscated_id)
        self.storage.remove(db_assignment)
        self.storage.commit()


class TokenController:

    def __init__(self, storage: Session):
        self.storage = storage

    def clean_expired_tokens(self):
        self.storage.query(InternalToken).filter(InternalToken.expires < datetime.utcnow()).delete()

    def remove_tokens_for_user(self, user: InternalUser):
        self.storage.query(InternalToken).filter(InternalToken.user_id == user.id).delete()
        self.storage.commit()

    def delete_token(self, token: str):
        matches = self.storage.query(InternalToken).filter_by(token=token)
        self.storage.remove(matches)
        self.storage.commit()

    def check_token(self, token) -> bool:
        self.clean_expired_tokens()
        return self.storage.query(InternalToken).filter(InternalToken.expires > datetime.utcnow()).filter_by(
            token=token).count() == 1

    def get_user_from_token(self, token: str) -> InternalUser:
        matches = self.storage.query(InternalToken).filter_by(token=token)
        if matches.count() > 0:
            return matches.first().user

    def get_token_for_user(self, user: InternalUser) -> InternalToken:
        self.clean_expired_tokens()
        matches = self.storage.query(InternalToken).filter_by(user_id=user.id)
        if matches.count() > 0:
            return matches.first()

    def get_or_create_token_for_user(self, user: InternalUser) -> InternalToken:
        token = self.get_token_for_user(user)
        if not token:
            return self.issue_token(user)
        return token

    def issue_token(self, to_user: InternalUser) -> InternalToken:
        key = None
        while True:
            self.storage.begin(subtransactions=True)
            key = ''.join(random.choice(
                string.ascii_uppercase + string.ascii_lowercase + string.digits) for _ in range(16)
                          )
            if self.check_token(key):
                self.storage.rollback()
                continue
            t = InternalToken(user_id=to_user.id, expires=datetime.now() + timedelta(days=7), token=key)
            self.storage.add(t)
            self.storage.commit()
            break

        return self.storage.query(InternalToken).filter_by(token=key).first()


class UserController:

    def __init__(self, storage):
        self.storage = storage

    def get_administrators(self):
        users = self.get_all_users().filter_by(role="Administrator")
        for u in users:
            yield u

    def initial_user_created(self) -> bool:
        for user in self.get_administrators():
            logging.debug(user)
            return True
        return False

    def get_user_from_id(self, id: int) -> InternalUser:
        return self.get_all_users().filter_by(id=id).first()

    def get_all_users(self):
        return self.storage.query(InternalUser).filter_by(deactivated_on=None)

    def check_credentials(self, lr: LoginRequest) -> bool:
        user = self.get_user(lr.username)
        return bcrypt.checkpw(lr.password.encode("utf8"), user.password)

    def get_user(self, username: str) -> InternalUser:
        return self.get_all_users().filter_by(username=username).first()

    def _create_user(self, rq: NewUserRequest, reset_needed=False) -> ValidationError:
        # Check for obvious issues like missing fields
        if False:
            errors = rq.check_for_problems()
            if len(errors) > 0:
                return errors

        # Encrypt, salt user password
        password_hash = bcrypt.hashpw(rq.password.encode("utf8"), bcrypt.gensalt())

        u = InternalUser(
            username=rq.username,
            role=rq.role.value,
            password=password_hash,
            email=rq.email,
            random_seed=bcrypt.gensalt(),
            password_reset_needed=reset_needed,
            deactivated_on=None,
        )

        self.storage.add(u)
        self.storage.commit()

        # self.storage.add(u)
        # self.storage.commit()
        return None

    def create_user(self, new_user: NewUserRequest, requesting_user: InternalUser):
        if requesting_user.role != UserKind.ADMINISTRATOR.value:
            return ValidationError([FieldError("_meta", "requesting user is not administrator", False)])

        return self._create_user(new_user, reset_needed=True)

    def create_initial_user(self, rq) -> ValidationError:
        if self.initial_user_created():
            return ValidationError([FieldError("_state", "initial user already created", False)])

        rq.role = UserKind.ADMINISTRATOR
        return self._create_user(rq, reset_needed=False)

    def change_password(self, user: InternalUser, old_password: str, new_password: str,
                        check_password: bool) -> ValidationError:
        if check_password:
            # Check the credentials
            status = bcrypt.checkpw(old_password.encode("utf8"), user.password)
            if not status:
                return ValidationError([FieldError("password", "must match original", False)])

        new_hash = bcrypt.hashpw(new_password.encode("utf8"), bcrypt.gensalt())
        user.password = new_hash
        user.password_reset_needed = not check_password
        self.storage.commit()

        t = TokenController(self.storage)
        t.remove_tokens_for_user(user)


class CorpusController:

    def __init__(self, storage: Session):
        self.storage = storage

    def get_identifiers(self) -> [str]:
        return [x.name for x in self.storage.query(InternalCorpus).all()]

    def get_corpus_from_identifier(self, id: str) -> InternalCorpus:
        return self.storage.query(InternalCorpus).filter_by(name=id).first()

    def create_corpus(self, c: Corpus):
        c = InternalCorpus(
            name=c.name,
            description=c.description,
            copyright_usage_restrictions=c.copyright
        )
        self.storage.add(c)
        self.storage.commit()


class AssetController:
    def __init__(self, storage: Session):
        self.storage = storage

    def convert_to_external(self, asset: InternalAsset) -> BinaryAssetDescription:
        """
        Converts an `Asset` to an external representation.
        :param asset: The `Asset` to convert.
        :return: A `BinaryAssetDescription`
        """
        return BinaryAssetDescription(
            asset.mime_type,
            BinaryAssetKind(asset.type_description),
            asset.copyright_usage_restrictions,
            asset.checksum,
            obfuscate_int64_field(asset.uploader_id),
            asset.date_uploaded,
            obfuscate_int64_field(asset.id),
            asset.user_metadata
        )

    def get_asset_with_corpus(self, c: InternalCorpus, id: str) -> InternalAsset:
        """
        Retrieves an `Asset` from the database.
        :param c: An internal Corpus object
        :param id: The unique name of the Asset inside c
        :return: An InternalAsset
        """
        return self.storage.query(InternalAsset).filter_by(corpus=c).filter_by(name=id).first()

    def get_asset_with_id(self, id: int) -> InternalAsset:
        """
        Retrieves an `Asset` from the database with an identifier.
        :param id:
        :return:
        """
        return self.storage.query(InternalAsset).get(id)

    def create_asset(self, a: BinaryAsset, c: InternalCorpus, id: str, uploader: InternalUser) -> ValidationError:
        """
        Creates an asset from the external representation and saves it.
        :param a: An external representation (BinaryAsset)
        :param c: An internal Corpus object
        :param id: The distinct name for this asset in corpus c
        :param uploader: The user who's uploading the asset.
        :return: ValidationError if something went wrong.
        """
        c = InternalAsset(
            name=id,
            content=a.content,
            user_metadata=a.metadata,
            copyright_usage_restrictions=a.copyright,
            checksum=a.checksum,
            mime_type=a.mime_type,
            type_description=a.type_description.value,
            corpus_id=c.id,
            uploader_id=uploader.id
        )

        self.storage.add(c)
        self.storage.commit()

    def delete_asset(self, which: InternalAsset):
        self.storage.delete(which)
        self.storage.commit()
        return True


class QuestionController:
    def __init__(self, storage):
        self.storage = storage

    def create_question(self, creator: InternalUser, corpus: InternalCorpus,
                        question: AbstractQuestion) -> SuccessfulInsert:
        iq = InternalQuestion(content=question.to_json(), kind=question.kind.value, creator_id=creator.id,
                              corpus_id=corpus.id, summary_code=question.summary_code)
        self.storage.add(iq)
        self.storage.commit()

        return SuccessfulInsert(iq.id)

    def retrieve_questions(self, corpus: InternalCorpus):
        return self.storage.query(InternalQuestion).filter_by(corpus=corpus).all()

    def retrieve_question(self, corpus: InternalCorpus, question_id: int) -> InternalQuestion:
        return self.storage.query(InternalQuestion).filter_by(corpus=corpus).filter_by(id=question_id).first()

    def delete_question(self, question: InternalQuestion):
        self.storage.delete(question)
        self.storage.commit()

    @classmethod
    def convert_to_external(cls, q: InternalQuestion) -> AbstractQuestion:
        ret = Question.from_json(q.content)
        ret.id = obfuscate_int64_field(q.id)
        return ret


class CurrentUserResource:

    def on_get(self, req, resp):  # getWhoIAm
        if not req.user:
            resp.status = falcon.HTTP_202
            return resp

        user = AnnotatronUser(username=req.user.username, role=UserKind(req.user.role),
                              created=req.user.created, id=req.obfuscate_int64_field(req.user.id),
                              email=req.user.email)
        resp.media = user


class InitialUserResource:
    """
    Test:
        * Empty database, on_get should return "I need configuration."
        * On post, should accept user response
        * on_get should no longer return "I need configuration."
        * on_post should no longer accept new users
    """

    def on_get(self, req, resp):  # checkNeedsSetup
        initial = not UserController(req.session).initial_user_created()
        response = ConfigurationResponse(requires_setup=(initial))
        resp.obj = response
        resp.status = falcon.HTTP_200
        return resp

    def on_post(self, req, resp):  # createInitialUser
        u = UserController(req.session)
        rq = NewUserRequest.from_json(req.body)
        errors = u.create_initial_user(rq)
        if errors:
            resp.obj = errors
            resp.status = falcon.HTTP_403
            # resp.status = falcon.HTTPNotAcceptable
        else:
            t = TokenController(req.session)
            user = u.get_user(rq.username)
            token = t.get_or_create_token_for_user(user)
            resp.obj = LoginResponse(token=token.token, password_reset_needed=user.password_reset_needed)
            resp.status = falcon.HTTP_201
        return resp


class WhoAmIResource:

    def on_get(self, req, resp):  # getWhoIAm
        obfuscated_id = req.obfuscate_int64_field(req.user.id)
        redirect = "/auth/users/{}".format(obfuscated_id)
        raise falcon.HTTPFound(redirect)


class UserResource:

    def on_get(self, req, resp, id=None):  # getUserDetails # listUsers
        if id:
            return self._get_id(req, resp, id)
        return self._get_all(req, resp)

    def _get_all(self, req, resp):
        user_list = UserController(req.session).get_all_users()
        ret = []
        for u in user_list:
            ret.append(req.obfuscate_int64_field(u.id))
        resp.obj = ret

    def _get_id(self, req, resp, id):
        original_id = req.recover_int64_field(id)
        if req.user.role == UserKind.ADMINISTRATOR.value:
            u = UserController(req.session).get_user_from_id(original_id)
            response = AnnotatronUser(
                username=u.username,
                email=u.email,
                role=UserKind(u.role),
                created=u.created,
                id=id,
            )
            resp.obj = response
        elif original_id == req.user.id:
            response = AnnotatronUser(
                username=req.user.username,
                email=req.user.email,
                role=UserKind(req.user.role),
                created=req.user.created,
                id=id,
            )
            resp.obj = response
        else:
            raise falcon.HTTPForbidden()

    def on_post(self, req, resp):  # createUser
        u = UserController(req.session)
        rq = NewUserRequest.from_json(req.body)
        errors = u.create_user(rq, req.user)
        if errors:
            resp.obj = errors
            resp.status = falcon.HTTP_403
            # resp.status = falcon.HTTPNotAcceptable
        else:
            t = TokenController(req.session)
            user = u.get_user(rq.username)
            token = t.get_or_create_token_for_user(user)
            resp.obj = LoginResponse(token=token.token, password_reset_needed=user.password_reset_needed)
            resp.status = falcon.HTTP_201
        return resp


class UserPasswordResource:

    def on_put(self, req, resp, id):
        original_id = req.recover_int64_field(id)
        if req.user.role == UserKind.ADMINISTRATOR.value or original_id == req.user.id:
            # User's changing their own password, or they're an administrator
            if "oldPassword" in req.body:
                old_password = req.body["oldPassword"]
            else:
                old_password = None
            new_password = req.body["newPassword"]

            should_check_password = req.user.id == original_id

            u = UserController(req.session)
            user = u.get_user_from_id(original_id)
            errors = u.change_password(user, old_password, new_password, should_check_password)
            if errors:
                resp.obj = errors
                raise falcon.HTTPForbidden()
            resp.status = falcon.HTTP_202
        else:
            raise falcon.HTTPForbidden()


class TokenResource:

    def on_post(self, req, resp):
        u = UserController(req.session)
        t = TokenController(req.session)
        l = LoginRequest.from_json(req.body)

        if u.check_credentials(l):
            user = u.get_user(l.username)
            resp.status = falcon.HTTP_200
            resp.obj = LoginResponse(token=t.get_or_create_token_for_user(user).token,
                                     password_reset_needed=user.password_reset_needed)
        else:
            resp.status = falcon.HTTP_403
            resp.media = None
        return resp

    def on_delete(self, req, resp):
        t = TokenController(req.session)
        t.delete_token(req.token)
        resp.status = falcon.HTTP_202


class CorpusResource:

    def list_all_corpora(self, req, resp, c):
        resp.obj = c.get_identifiers()

    def get_corpus_by_id(self, req, resp, id, obj):

        resp.obj = Corpus(obj.name,
                          obj.description,
                          obj.created,
                          obj.copyright_usage_restrictions)

    def get_assets_by_corpus_id(self, req, resp, corpus):
        assets = corpus.assets
        resp.obj = [x.name for x in assets]

    def get_asset_info_with_id(self, req, resp, corpus, id: str):
        controller = AssetController(req.session)
        asset = controller.get_asset_with_corpus(corpus, id)
        resp.obj = controller.convert_to_external(asset)

    def on_get(self, req, resp, corpus_id=None, corpus_property=None, property_value=None):
        if req.user.role != UserKind.ADMINISTRATOR.value and req.user.role != UserKind.STAFF.value:
            raise falcon.HTTPForbidden("Must be admin or staff")

        routed = False
        c = CorpusController(req.session)
        if not corpus_id:
            self.list_all_corpora(req, resp, c)
            routed = True
        else:
            corpus = c.get_corpus_from_identifier(corpus_id)
            if not corpus_property:
                self.get_corpus_by_id(req, resp, corpus_id, corpus)
                routed = True
            else:
                if corpus_property == "assets":
                    routed = True
                    if not property_value:
                        self.get_assets_by_corpus_id(req, resp, corpus)
                    else:
                        self.get_asset_info_with_id(req, resp, corpus, property_value)
                elif corpus_property == "questions":
                    if not property_value:
                        self.get_questions(req, resp, corpus)
                    else:
                        self.get_question(req, resp, corpus, property_value)

        if not routed:
            raise falcon.HTTPNotFound()

    def on_delete(self, req, resp, corpus_id: str, corpus_property: str, property_value: str):  # deleteAssetWithId
        if req.user.role != UserKind.ADMINISTRATOR.value and req.user.role != UserKind.STAFF.value:
            raise falcon.HTTPForbidden("Must be admin or staff")

        if corpus_property == "assets":
            self.delete_asset_with_id(req, resp, corpus_id, property_value)
        elif corpus_property == "questions":
            self.delete_question_with_id(req, resp, corpus_id, property_value)
        else:
            raise falcon.HTTPNotFound()

    def delete_asset_with_id(self, req, resp, corpus_id: str, asset_id: str):
        corpus_controller = CorpusController(req.session)
        asset_controller = AssetController(req.session)

        corpus = corpus_controller.get_corpus_from_identifier(corpus_id)
        asset = asset_controller.get_asset_with_corpus(corpus, asset_id)
        if not asset_controller.delete_asset(asset):
            raise falcon.HTTPInternalServerError("Could not delete Asset")
        resp.status = falcon.status.HTTP_ACCEPTED

    def create_asset(self, req, resp, corpus_id: str, asset_id: str):
        corpus_controller = CorpusController(req.session)
        asset_controller = AssetController(req.session)
        destination_corpus = corpus_controller.get_corpus_from_identifier(corpus_id)
        new_asset = BinaryAsset.from_json(req.body)
        asset_controller.create_asset(new_asset, destination_corpus, asset_id, req.user)
        resp.status = falcon.HTTP_201

    def create_corpus(self, req, resp):
        c = CorpusController(req.session)
        new_corpus = Corpus.from_json(req.body)
        c.create_corpus(new_corpus)
        resp.status = falcon.HTTP_201

    def delete_question_with_id(self, req, resp, corpus_id: str, question_id: str):
        corpus_controller = CorpusController(req.session)
        question_controller = QuestionController(req.session)

        question_id = recover_int64_field(int(question_id))

        corpus = corpus_controller.get_corpus_from_identifier(corpus_id)
        question = question_controller.retrieve_question(corpus, int(question_id))
        question_controller.delete_question(question)
        resp.status = falcon.HTTP_202

    def create_question(self, req, resp, corpus_id: str):
        question = Question.from_json(req.body)
        qc = QuestionController(req.session)
        cc = CorpusController(req.session)

        corpus = cc.get_corpus_from_identifier(corpus_id)
        resp.obj = qc.create_question(req.user, corpus, question)
        resp.obj.id = req.obfuscate_int64_field(resp.obj.id)
        resp.status = falcon.HTTP_201

    def get_questions(self, req, resp, corpus: InternalCorpus):
        qc = QuestionController(req.session)
        resp.obj = [obfuscate_int64_field(q.id) for q in qc.retrieve_questions(corpus)]

    def get_question(self, req, resp, corpus: InternalCorpus, question_id: int):
        qc = QuestionController(req.session)
        question_id = req.recover_int64_field(int(question_id))
        resp.obj = qc.convert_to_external(qc.retrieve_question(corpus, question_id))

    def on_post(self, req, resp, corpus_id: str = None, corpus_property: str = None, property_value: str = None):
        if req.user.role != UserKind.ADMINISTRATOR.value and req.user.role != UserKind.STAFF.value:
            raise falcon.HTTPForbidden("Must be admin or staff")

        if not corpus_id:
            self.create_corpus(req, resp)
        elif corpus_property == "assets" and property_value:
            self.create_asset(req, resp, corpus_id, property_value)
        elif corpus_property == "questions":
            self.create_question(req, resp, corpus_id)
        else:
            raise falcon.HTTPNotFound()


class AssetResource:

    def on_get(self, req, resp, asset_id):
        if req.user is None:
            raise falcon.HTTP_FORBIDDEN("Must be logged in")
        asset_controller = AssetController(req.session)
        asset = asset_controller.get_asset_with_id(req.recover_int64_field(asset_id))
        resp.content_type = asset.mime_type
        resp.body = asset.content

        if asset.type_description == BinaryAssetKind.UTF8_TEXT.value:
            resp.encoding = "utf8"


class AssignmentResource:

    def on_post(self, req, resp, arg1: str):
        corpus_name = arg1
        new_assignment = Assignment.from_json(req.body)
        corpus_controller = CorpusController(req.session)
        corpus = corpus_controller.get_corpus_from_identifier(corpus_name)
        assignment_controller = AssignmentController(req.session)
        insert, error = assignment_controller.create_assigment(new_assignment, corpus)
        if insert:
            resp.obj = insert
            resp.status = falcon.HTTP_CREATED
        else:
            resp.obj = error
            resp.status = falcon.HTTP_NOT_ACCEPTABLE

    def on_patch(self, req, resp, arg1, arg2):
        key = None
        if req.user.role != UserKind.ADMINISTRATOR.value \
                and req.user.role != UserKind.STAFF.value:
            key = req.user.random_seed

        database_id = req.recover_int64_field(arg1, key)
        assignment_controller = AssignmentController(req.session)
        decoded_assigment = AssignmentResponse.from_json(req.body)

        if arg2 not in ["submit", "approve", "reject"]:
            resp.obj = ValidationError(FieldError("action", "must be [submit, approve, reject]"))
            raise falcon.HTTPNotAcceptable()

        assignment_controller.update_assignment(database_id, decoded_assigment, req.user, arg2)
        resp.status = falcon.HTTP_ACCEPTED

    def on_get(self, req, resp, arg1, arg2=None):
        assignment_controller = AssignmentController(req.session)
        if arg1 == "byUser":
            if not arg2:
                raise falcon.HTTPNotFound()
            if req.user.role != UserKind.ADMINISTRATOR.value \
                    and req.user.role != UserKind.STAFF.value:
                if arg2 != req.user.id:
                    raise falcon.HTTPForbidden()
                key = req.user.random_seed
            else:
                key = None
            arg2 = req.recover_int64_field(arg2)
            response = assignment_controller.retrieve_assignments_for_user(arg2)

            ret = {
                "forReview": [],
                "forAnnotation": []
            }
            for r in response:
                if r.reviewer_id == req.user.id:
                    ret["forReview"].append(req.obfuscate_int64_field(r.id, key))
                else:
                    ret["forAnnotation"].append(req.obfuscate_int64_field(r.id, key))

            resp.obj = ret
        elif arg1 == "byCorpus":
            if not arg2:
                raise falcon.HTTPNotFound()
            if req.user.role != UserKind.ADMINISTRATOR.value and req.user.role != UserKind.STAFF.value:
                raise falcon.HTTPForbidden()
            corpus_controller = CorpusController(req.session)
            corpus = corpus_controller.get_corpus_from_identifier(arg2)
            assignment_controller = AssignmentController(req.session)
            response = assignment_controller.retrieve_assignments_for_corpus(corpus)

            ret = {
                "forReview": [],
                "forAnnotation": [],
                "completed": []
            }
            for r in response:
                if r.state == "approved":
                    ret["completed"].append(req.obfuscate_int64_field(r.id))
                elif r.state == "pending":
                    ret["forReview"].append(req.obfuscate_int64_field(r.id))
                else:
                    ret["forAnnotation"].append(req.obfuscate_int64_field(r.id))
            resp.obj = ret
        else:
            if req.user.role != UserKind.ADMINISTRATOR.value \
                    and req.user.role != UserKind.STAFF.value:
                key = req.user.random_seed
            else:
                key = None
            assignment_id = req.recover_int64_field(arg1, key)
            assignment = assignment_controller.retrieve_assignment(assignment_id)
            resp.obj = assignment_controller.convert(assignment)


class GetSessionTokenComponent:

    def process_request(self, req, resp):
        auth = req.get_header('Authorization')
        if auth:
            method, _, auth = auth.partition(' ')
            if method != 'Bearer':
                raise falcon.HTTPNotAcceptable('This API only supports Bearer Authorization')

            t = TokenController(req.session)
            req.user = t.get_user_from_token(auth)Q
            req.token = auth

        # TODO: restrict URL choice in here


class ObfuscationComponent:

    def process_request(self, req, resp):
        # TODO: make me an environmnent variable
        def obfuscate_int64_field(x, key=None):
            if not key:
                key = "habppootle"
            cipher = ARC4.new(key)
            cipher.encrypt(b'\xbe\x89\xd0\xb1 \xb8\x99\xbd')
            return int.from_bytes(cipher.encrypt((x).to_bytes(8, byteorder='big')), 'big')

        def recover_int64_field(x, key=None):
            if not key:
                key = "habppootle"
            cipher = ARC4.new(key)
            cipher.encrypt(b'\xbe\x89\xd0\xb1 \xb8\x99\xbd')
            return int.from_bytes(cipher.decrypt((int(x)).to_bytes(8, byteorder='big')), byteorder='big')

        req.obfuscate_int64_field = obfuscate_int64_field
        req.recover_int64_field = recover_int64_field


class AttachSessionComponent:

    def process_request(self, req, resp):
        req.session = Session()


class RequireJSONComponent(object):

    def process_request(self, req, resp):
        if not req.client_accepts_json:
            raise falcon.HTTPNotAcceptable(
                'This API only supports responses encoded as JSON.',
                href='http://docs.examples.com/api/json')

        if req.method in ('POST', 'PUT'):
            if 'application/json' not in req.content_type:
                raise falcon.HTTPUnsupportedMediaType(
                    'This API only supports requests encoded as JSON.',
                    href='http://docs.examples.com/api/json')


class JSONTranslatorComponent(object):
    # NOTE: Starting with Falcon 1.3, you can simply
    # use req.media and resp.media for this instead.

    def process_request(self, req, resp):
        # req.stream corresponds to the WSGI wsgi.input environ variable,
        # and allows you to read bytes from the request body.
        #
        # See also: PEP 3333
        if req.content_length in (None, 0):
            # Nothing to do
            return

        body = req.stream.read()
        if not body:
            raise falcon.HTTPBadRequest('Empty request body',
                                        'A valid JSON document is required.')

        try:
            req.body = json.loads(body.decode('utf-8'))

        except (ValueError, UnicodeDecodeError):
            raise falcon.HTTPError(falcon.HTTP_753,
                                   'Malformed JSON',
                                   'Could not decode the request body. The '
                                   'JSON was incorrect or not encoded as '
                                   'UTF-8.')

    def process_response(self, req, resp, resource):
        try:
            if resp.obj is not None:
                try:
                    if type(resp.obj) == list:
                        resp.body = json.dumps([x.to_json() for x in resp.obj])
                    else:
                        resp.body = json.dumps(resp.obj.to_json())
                except AttributeError:
                    resp.body = json.dumps(resp.obj)
                resp.content_type = falcon.MEDIA_JSON
        except AttributeError:
            pass


def create_app(engine=None):
    if not engine:
        engine = create_engine("postgresql+psycopg2://annotatron:annotatron@localhost/annotatron", echo=True)
    Session.configure(bind=engine)
    app = falcon.API(middleware=[AttachSessionComponent(), JSONTranslatorComponent(), RequireJSONComponent(),
                                 ObfuscationComponent(), GetSessionTokenComponent()])
    app.add_route("/conf/initialUser", InitialUserResource())
    app.add_route("/auth/token", TokenResource())
    app.add_route("/auth/whoAmI", WhoAmIResource())
    app.add_route("/auth/users", UserResource())
    app.add_route("/auth/users/{id}", UserResource())
    app.add_route("/auth/users/{id}/password", UserPasswordResource())
    app.add_route("/corpus", CorpusResource())
    app.add_route("/corpus/{corpus_id}", CorpusResource())
    app.add_route("/corpus/{corpus_id}/{corpus_property}", CorpusResource())
    app.add_route("/corpus/{corpus_id}/{corpus_property}/{property_value}", CorpusResource())
    app.add_route("/asset/{asset_id:int}/content", AssetResource()),
    app.add_route("/assignments/{arg1}/{arg2}", AssignmentResource()),
    app.add_route("/assignments/{arg1}", AssignmentResource()),

    return app


app = create_app()

if __name__ == '__main__':
    httpd = simple_server.make_server('127.0.0.1', 8000, app)
    httpd.serve_forever()
