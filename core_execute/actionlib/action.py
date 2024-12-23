from typing import Any, Self
import traceback
import sys
import os
import enum
import core_logging as log

from core_framework.models import ActionDefinition, DeploymentDetails, ActionParams

from core_renderer import Jinja2Renderer

from core_framework.status import RELEASE_IN_PROGRESS

from core_db.dbhelper import update_status, update_item


ACT_LABEL = "Label"
ACT_TYPE = "Type"
ACT_CONDITION = "Condition"
ACT_BEFORE = "Before"
ACT_AFTER = "After"
ACT_PARAMS = "Params"
ACT_LIFECYCLE_HOOKS = "LifecycleHooks"
ACT_SAVE_OUTPUTS = "SaveOutputs"
ACT_DEPENDS_ON = "DependsOn"
ACT_STATUS_HOOOK = "StatusHook"

STATUS_CODE = "StatusCode"
STATUS_REASON = "StatusReason"

LC_TYPE_STATUS = "status"
LC_HOOK_PENDING = "Pending"
LC_HOOK_FAILED = "Failed"
LC_HOOK_RUNNING = "Running"
LC_HOOK_COMPLETE = "Complete"


class StatusCode(enum.Enum):
    """Enum for action status codes."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"


class BaseAction:

    context: dict[str, Any]
    label: str
    action_name: str
    output_namespace: str | None
    state_namespace: str
    type: str
    condition: str
    after: list[str]
    params: ActionParams
    lifecycle_hooks: list[dict[str, Any]]
    deployment_details: DeploymentDetails
    renderer: Jinja2Renderer

    def _execute(self):
        raise NotImplementedError("Must implement in subclass")

    def _check(self):
        raise NotImplementedError("Must implement in subclass")

    def _resolve(self):
        raise NotImplementedError("Must implement in subclass")

    def _cancel(self):
        raise NotImplementedError("Must implement in subclass")

    def _unexecute(self):
        raise NotImplementedError("Must implement in subclass")

    def __init__(
        self,
        definition: ActionDefinition,
        context: dict[str, Any],
        deployment_details: DeploymentDetails,
    ):
        # All actions can use the Jinja2 renderer to parse CloudFormation.j2 templates
        self.renderer = Jinja2Renderer()

        self.context = context
        self.deployment_details = deployment_details

        # Extract action details from the definition
        self.label = definition.Label
        self.action_name = self.label.split("/", 1)[-1]

        # Set output_namespace if user specified SaveOutputs = True
        if definition.SaveOutputs:
            self.output_namespace = self.label.split("/", 1)[0].replace(
                ":action", ":output"
            )
        else:
            self.output_namespace = None

        # State namespace is the same as action label, except with :var/ instead of :action/
        self.state_namespace = self.label.replace(":action/", ":var/")
        self.type = definition.Type
        self.condition = definition.Condition
        self.before = definition.Before
        self.after = definition.After + definition.DependsOn
        self.params = definition.Params
        self.lifecycle_hooks = definition.LifecycleHooks

    def is_init(self):
        return self.__get_status_code() == StatusCode.PENDING.value

    def is_failed(self):
        return self.__get_status_code() == StatusCode.FAILED.value

    def is_running(self):
        return self.__get_status_code() == StatusCode.RUNNING.value

    def is_complete(self):
        return self.__get_status_code() == StatusCode.COMPLETE.value

    def set_failed(self, reason: str):
        # Ignore duplicate state updates
        if self.is_failed() and self.__get_status_reason() == reason:
            return

        # Log the state change
        log.debug("Action has failed - {}", reason)

        # Execute lifecycle hooks
        self.__execute_lifecycle_hooks(LC_HOOK_FAILED, reason)

        # Update the context with the new state
        self.__set_context(self.label, STATUS_CODE, StatusCode.FAILED.value)
        self.__set_context(self.label, STATUS_REASON, reason)

    def set_running(self, reason: str):
        # Ignore duplicate state updates
        if self.is_running() and self.__get_status_reason() == reason:
            return

        # Log the state change
        log.debug(reason or "Action is running")

        # Execute lifecycle hooks
        self.__execute_lifecycle_hooks(LC_HOOK_RUNNING, reason)

        # Update the context with the new state
        self.__set_context(self.label, STATUS_CODE, StatusCode.RUNNING.value)
        self.__set_context(self.label, STATUS_REASON, reason)

    def set_complete(self, reason: str):
        # Ignore duplicate state updates
        if self.is_complete() and self.__get_status_reason() == reason:
            return

        # Log the state change
        log.debug("Action is complete - {}", reason)

        # Execute lifecycle hooks
        self.__execute_lifecycle_hooks(LC_HOOK_COMPLETE, reason)

        # Update the context with the new state
        self.__set_context(self.label, STATUS_CODE, StatusCode.COMPLETE.value)
        self.__set_context(self.label, STATUS_REASON, reason)

    def set_skipped(self, reason: str):
        # Ignore duplicate state updates
        if self.is_complete() and self.__get_status_reason() == reason:
            return

        # Log the state change
        log.debug("Action has been skipped - {}", reason)

        # Update the context with the new state
        self.__set_context(self.label, STATUS_CODE, StatusCode.COMPLETE.value)
        self.__set_context(self.label, STATUS_REASON, reason)

    def set_output(self, name: str, value: Any):
        # Set output variable (if user chose to save outputs)
        if self.output_namespace is not None:
            log.debug(
                "Setting output '{}/{}' = '{}'", self.output_namespace, name, value
            )
            self.__set_context(self.output_namespace, name, value)

        # Set state variable
        self.__set_context(self.state_namespace, name, value)

    def get_output(self, name: str) -> str | None:
        if self.output_namespace is None:
            return None
        return self.__get_context(self.output_namespace, name)

    def set_state(self, name: str, value: Any):
        self.__set_context(self.state_namespace, name, value)

    def get_state(self, name: str) -> str:
        return self.__get_context(self.state_namespace, name)

    def execute(self) -> Self:
        try:
            # Temporarily set the logger identity to this action's label
            log.set_identity(self.label)

            # Render the action condition, and see if it evaluates to true
            condition_result = self.renderer.render_string(
                "{{{{ {} }}}}".format(self.condition), self.context
            )

            if condition_result.lower() == "true":
                # Condition is true, execute the action
                self._resolve()
                self._execute()
            else:
                # Condition is false, skip the action
                self.set_skipped("Condition evaluated to '{}'".format(condition_result))

        except Exception as e:
            # Something went wrong (internal error)
            exc_type, exc_obj, exc_tb = sys.exc_info()
            if exc_type is None:
                exc_type = type(e)
            if exc_tb and exc_tb.tb_frame:
                fname = os.path.split(exc_tb.tb_frame.f_code.co_filename)[1]
                lineno = exc_tb.tb_lineno
            else:
                fname = "Unknown"
                lineno = -1
            tb_str = ''.join(traceback.format_exception(exc_type, exc_obj, exc_tb))
            self.set_failed(
                "Internal error {} in {} at {} - {}\nTraceback:\n{}".format(
                    exc_type.__name__, fname, lineno, str(e), tb_str
                )
            )

        finally:
            # Reset the logger identity to base value
            log.reset_identity()

        return self

    def check(self) -> Self:
        try:
            # Temporarily set the logger identity to this action's label
            log.set_identity(self.label)

            log.debug("Checking action for {}", self.label)

            self._resolve()
            self._check()

        except Exception as e:
            # Something went wrong (internal error)
            exc_type, exc_obj, exc_tb = sys.exc_info()
            if exc_type is None:
                exc_type = type(e)
            if exc_tb and exc_tb.tb_frame:
                fname = os.path.split(exc_tb.tb_frame.f_code.co_filename)[1]
                lineno = exc_tb.tb_lineno
            else:
                fname = "Unknown"
                lineno = -1
            tb_str = ''.join(traceback.format_exception(exc_type, exc_obj, exc_tb))
            self.set_failed(
                "Internal error {} in {} at {} - {}\nTraceback:\n{}".format(
                    exc_type.__name__, fname, lineno, str(e), tb_str
                )
            )

        finally:
            # Reset the logger identity to base value
            log.reset_identity()

        return self

    def __get_status_code(self):
        return self.__get_context(self.label, STATUS_CODE, StatusCode.PENDING.value)

    def __get_status_reason(self):
        return self.__get_context(self.label, STATUS_REASON, None)

    def __get_context(
        self, prn: str, name: str, default: str = "_!NO!DEFAULT!PROVIDED!_"
    ) -> str:
        key = "{}/{}".format(prn, name)

        if self.context and key in self.context:
            return self.context[key]

        else:
            if default == "_!NO!DEFAULT!PROVIDED!_":
                raise KeyError(
                    "Key '{}' is not in the context and no default was provided".format(
                        name
                    )
                )
            else:
                return default

    def __set_context(self, prn: str, name: str, value: Any):

        key = "{}/{}".format(prn, name)

        if not self.context:
            self.context = {key: value}
        else:
            self.context[key] = value

    def __execute_lifecycle_hooks(self, event: str, reason: str):
        # Retrieve the event hooks for this action, for this state event
        event_hooks = [
            h for h in self.lifecycle_hooks if event in h.get("States", [])
        ]

        # Execute the event hooks
        for event_hook in event_hooks:
            hook_type = event_hook["Type"]
            self.__execute_lifecycle_hook(event, hook_type, event_hook, reason)

    def __execute_lifecycle_hook(
        self, event: str, hook_type: str, hook: dict[str, Any], reason: str
    ):
        if hook_type == LC_TYPE_STATUS:
            self.__execute_status_hook(event, hook, reason)
        else:
            raise Exception("Unsupported hook type {}".format(hook_type))

    def __get_status_parameter(self, event: str, hook: dict[str, Any]) -> str | None:
        key = f"On{event}"
        parms = hook.get("Parameters", {})
        if key in parms:
            action = parms[key]
            if "Status" in action:
                return action["Status"]
        return None

    def __get_message_parameter(self, event, hook: dict[str, Any]) -> str | None:
        key = f"On{event}"
        parms = hook.get("Parameters", {})
        if key in parms:
            action = parms[key]
            if "Message" in action:
                return action["Message"]
        return None

    def __get_idenity_parameter(self, event: str, hook: dict[str, Any]) -> str | None:
        parms = hook.get("Parameters", hook)
        if "Identity" in parms:
            return parms["Identity"]
        return None

    def __get_details_parameter(self, event: str, hook: dict[str, Any]) -> dict | None:
        parms = hook.get("Parameters", hook)
        if "Details" in parms:
            return parms["Details"]
        return None

    def __update_item_status(
        self, identity: str, status: str, message: str, details: Any
    ):
        try:
            # Log the status
            log.set_identity(identity)

            prn_sections = identity.split(":")

            # Build PRN
            if len(prn_sections) == 5:
                build_prn = ":".join(prn_sections[0:5])

                # Update the build status
                update_status(
                    prn=build_prn, status=status, message=message, details=details
                )

                # If a new build is being released, update the branch's released_build_prn pointer
                if status == RELEASE_IN_PROGRESS:
                    branch_prn = ":".join(prn_sections[0:4])
                    update_item(prn=branch_prn, released_build_prn=build_prn)

            # Component PRN
            if len(prn_sections) == 6:
                component_prn = ":".join(prn_sections[0:6])

                # Update the component status
                update_status(
                    prn=component_prn, status=status, message=message, details=details
                )

                # If component has failed, update the build status to failed
                if "_FAILED" in status:
                    build_prn = ":".join(prn_sections[0:5])

                    # Update the build status
                    update_status(prn=build_prn, status=status)

        except Exception as e:
            log.warn("Failed to update status via API - {}", e)

        finally:
            log.reset_identity()

    def __execute_status_hook(
        self, event: str, hook: dict[str, Any], reason: str | None
    ):

        # Extract hook["Parameter"]["On<event>"]["Status"], then try hook["Status"]
        status = self.__get_status_parameter(event, hook)
        message = self.__get_message_parameter(event, hook)
        identity = self.__get_idenity_parameter(event, hook)
        details = self.__get_details_parameter(event, hook)

        # Render templated parameters if a template has been provided
        if status:
            status = self.renderer.render_string(status, self.context)
        if identity:
            identity = self.renderer.render_string(identity, self.context)
        if message:
            message = self.renderer.render_string(message, self.context)

        # Ensure a status was provided
        if not status:
            log.warn(
                "Internal - status hook was executed, but no status was defined for event",
                details={ACT_STATUS_HOOOK: hook},
            )
            return

        # Ensure the identity was provided
        if not identity:
            log.warn(
                "Internal - status hook was executed, but no identity was defined",
                details={ACT_STATUS_HOOOK: hook},
            )
            return

        # Append reason to the message
        if reason:
            message = f"{message} - {reason}" if message else reason

        # Still no message?  Then see if we can finally get one set.
        if not message:
            message = reason if reason else ""

        # Update the status of the item
        self.__update_item_status(identity, status, message, details)

    def __repr__(self):
        return "{}({})".format(type(self).__name__, self.label)

    def __str__(self):
        return "{}({})".format(type(self).__name__, self.label)