"""Enhanced Background Job for Welcome Wizard with Image Import Support."""

import contextlib
import os
import re
from collections import OrderedDict
from django.core.files import File

from nautobot.apps.jobs import Job, StringVar
from nautobot.core.celery import register_jobs
from nautobot.dcim.forms import DeviceTypeImportForm
from nautobot.dcim.models import (
    ConsolePortTemplate,
    ConsoleServerPortTemplate,
    DeviceBayTemplate,
    DeviceType,
    FrontPortTemplate,
    InterfaceTemplate,
    Manufacturer,
    ModuleBayTemplate,
    PowerOutletTemplate,
    PowerPortTemplate,
    RearPortTemplate,
)
from nautobot.extras.models import GitRepository

from welcome_wizard.models.importer import DeviceTypeImport

COMPONENTS = OrderedDict()
COMPONENTS["console-ports"] = ConsolePortTemplate
COMPONENTS["console-server-ports"] = ConsoleServerPortTemplate
COMPONENTS["power-ports"] = PowerPortTemplate
COMPONENTS["power-outlets"] = PowerOutletTemplate
COMPONENTS["interfaces"] = InterfaceTemplate
COMPONENTS["rear-ports"] = RearPortTemplate
COMPONENTS["front-ports"] = FrontPortTemplate
COMPONENTS["device-bays"] = DeviceBayTemplate
COMPONENTS["module-bays"] = ModuleBayTemplate

STRIP_KEYWORDS = {
    "interfaces": ["poe_mode", "poe_type"],
}


def slugify_device_name(manufacturer_name, model_name):
    """
    Create a slug for device image lookup following devicetype-library convention.
    
    Converts "Palo Alto" + "PA-850" to "palo-alto-pa-850"
    """
    # Combine manufacturer and model
    combined = f"{manufacturer_name} {model_name}"
    
    # Convert to lowercase
    slug = combined.lower()
    
    # Replace spaces and underscores with hyphens
    slug = re.sub(r'[\s_]+', '-', slug)
    
    # Remove any characters that aren't alphanumeric or hyphens
    slug = re.sub(r'[^a-z0-9\-]', '', slug)
    
    # Remove consecutive hyphens
    slug = re.sub(r'-+', '-', slug)
    
    # Strip leading/trailing hyphens
    slug = slug.strip('-')
    
    return slug


def find_elevation_image(git_repo, manufacturer_name, model_name, image_type='front'):
    """
    Find elevation image file in the Git repository.
    
    Args:
        git_repo: GitRepository object
        manufacturer_name: Manufacturer name (e.g., "Palo Alto")
        model_name: Device model (e.g., "PA-850")
        image_type: 'front' or 'rear'
    
    Returns:
        Full path to image file if found, None otherwise
    """
    # Get the filesystem path of the Git repository
    repo_path = git_repo.filesystem_path
    
    # Create the slug for the device
    slug = slugify_device_name(manufacturer_name, model_name)
    
    # Construct the expected directory path
    elevation_dir = os.path.join(repo_path, 'elevation-images', manufacturer_name)
    
    if not os.path.exists(elevation_dir):
        return None
    
    # Common image extensions
    extensions = ['.png', '.jpg', '.jpeg', '.svg']
    
    # Try to find the image file
    # Pattern: <slug>.<front|rear>.<ext>
    for ext in extensions:
        filename = f"{slug}.{image_type}{ext}"
        full_path = os.path.join(elevation_dir, filename)
        
        if os.path.exists(full_path):
            return full_path
    
    return None


def import_device_type_with_images(data, git_repo=None, logger=None):
    """
    Import DeviceType with image support.
    
    Args:
        data: Device type data dictionary from YAML
        git_repo: GitRepository object (optional, for image import)
        logger: Job logger for output
    
    Returns:
        DeviceType object
    """
    manufacturer = Manufacturer.objects.get(name=data.get("manufacturer"))
    model = data.get("model")
    
    # Check if device type already exists
    with contextlib.suppress(DeviceType.DoesNotExist):
        devtype = DeviceType.objects.get(model=model, manufacturer=manufacturer)
        raise ValueError(
            f"Unable to import this device_type, a DeviceType with this model ({model}) "
            f"and manufacturer ({manufacturer}) already exists."
        )
    
    # Create the device type using the form
    dtif = DeviceTypeImportForm(data)
    devtype = dtif.save()
    
    if logger:
        logger.info(f"Created DeviceType: {manufacturer} {model}", extra={"object": devtype})
    
    # Import components
    for key, component_class in COMPONENTS.items():
        if key in data:
            component_list = [
                component_class(
                    device_type=devtype,
                    **{k: v for k, v in item.items() if k not in STRIP_KEYWORDS.get(key, [])},
                )
                for item in data[key]
            ]
            component_class.objects.bulk_create(component_list)
            
            if logger:
                logger.info(f"Created {len(component_list)} {key} for {model}")
    
    # Import images if Git repository is provided
    if git_repo:
        manufacturer_name = data.get("manufacturer")
        
        # Handle front image
        if data.get("front_image"):
            front_image_path = find_elevation_image(git_repo, manufacturer_name, model, 'front')
            if front_image_path:
                try:
                    with open(front_image_path, 'rb') as img_file:
                        devtype.front_image.save(
                            os.path.basename(front_image_path),
                            File(img_file),
                            save=True
                        )
                    if logger:
                        logger.info(f"Imported front image: {os.path.basename(front_image_path)}")
                except Exception as e:
                    if logger:
                        logger.warning(f"Failed to import front image: {e}")
            else:
                if logger:
                    logger.warning(f"Front image flagged but not found for {manufacturer_name} {model}")
        
        # Handle rear image
        if data.get("rear_image"):
            rear_image_path = find_elevation_image(git_repo, manufacturer_name, model, 'rear')
            if rear_image_path:
                try:
                    with open(rear_image_path, 'rb') as img_file:
                        devtype.rear_image.save(
                            os.path.basename(rear_image_path),
                            File(img_file),
                            save=True
                        )
                    if logger:
                        logger.info(f"Imported rear image: {os.path.basename(rear_image_path)}")
                except Exception as e:
                    if logger:
                        logger.warning(f"Failed to import rear image: {e}")
            else:
                if logger:
                    logger.warning(f"Rear image flagged but not found for {manufacturer_name} {model}")
    
    # Handle custom fields if they exist in the data
    custom_field_mapping = {
        'cf_slug': 'slug',
        'cf_weight': 'weight',
        'cf_weight_unit': 'weight_unit',
        'cf_airflow': 'airflow',
        'cf_front_image': 'front_image',
        'cf_rear_image': 'rear_image',
    }
    
    cf_data = {}
    for cf_key, data_key in custom_field_mapping.items():
        if data_key in data:
            cf_data[cf_key] = data[data_key]
    
    if cf_data:
        try:
            # Set custom field values
            for cf_key, value in cf_data.items():
                devtype.cf[cf_key] = value
            devtype.save()
            
            if logger:
                logger.info(f"Set custom fields: {list(cf_data.keys())}")
        except Exception as e:
            if logger:
                logger.warning(f"Failed to set custom fields: {e}")
    
    return devtype


name = "Welcome Wizard"  # pylint: disable=invalid-name


class WelcomeWizardImportManufacturer(Job):
    """Manufacturer Import."""

    class Meta:  # pylint: disable=too-few-public-methods
        """Meta for Manufacturer Import."""

        name = "Welcome Wizard - Import Manufacturer"
        description = "Imports a chosen Manufacturer (Run from the Welcome Wizard Dashboard)"

    manufacturer_name = StringVar(description="Name of the new manufacturer")

    def run(self, manufacturer_name):  # pylint: disable=arguments-differ
        """Tries to import the selected Manufacturer into Nautobot."""
        # Create the new manufacturer
        manufacturer, created = Manufacturer.objects.update_or_create(
            name=manufacturer_name,
        )
        action = "Created" if created else "Updated"
        self.logger.info(f"{action} manufacturer", extra={"object": manufacturer})


class WelcomeWizardImportDeviceType(Job):
    """Device Type Import with Image Support."""

    class Meta:  # pylint: disable=too-few-public-methods
        """Meta for Device Type Import."""

        name = "Welcome Wizard - Import Device Type"
        description = "Imports a chosen Device Type with elevation images (Run from the Welcome Wizard Dashboard)"

    filename = StringVar()

    def run(self, filename):  # pylint: disable=arguments-differ
        """Tries to import the selected Device Type into Nautobot with images."""
        device_type = filename if filename else "none.yaml"

        # Get the device type data from the import model
        device_type_import = DeviceTypeImport.objects.filter(filename=device_type).first()
        
        if not device_type_import:
            self.logger.error(f"Device type file not found: {device_type}")
            return
        
        device_type_data = device_type_import.device_type_data
        manufacturer_name = device_type_data.get("manufacturer")
        
        # Ensure manufacturer exists
        Manufacturer.objects.update_or_create(name=manufacturer_name)
        
        # Get the Git repository for image access
        git_repo = None
        try:
            git_repo = GitRepository.objects.get(slug="devicetype_library")
        except GitRepository.DoesNotExist:
            self.logger.warning(
                "Git repository 'devicetype_library' not found. "
                "Images will not be imported. Device type will still be created."
            )
        
        try:
            devtype = import_device_type_with_images(
                device_type_data,
                git_repo=git_repo,
                logger=self.logger
            )
        except ValueError as exc:
            self.logger.error(
                f"Unable to import {device_type}, a DeviceType with this model "
                f"and manufacturer ({manufacturer_name}) already exists. {exc}"
            )
            raise exc
        
        self.logger.info(
            f"Successfully imported DeviceType {device_type_data.get('model')} with all components",
            extra={"object": devtype}
        )


register_jobs(WelcomeWizardImportManufacturer, WelcomeWizardImportDeviceType)
