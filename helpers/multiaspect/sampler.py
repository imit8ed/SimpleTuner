import torch, logging, json, random, os
from io import BytesIO
from PIL import Image
from PIL.ImageOps import exif_transpose
from helpers.multiaspect.bucket import BucketManager
from helpers.multiaspect.state import BucketStateManager
from helpers.data_backend.base import BaseDataBackend
from helpers.training.state_tracker import StateTracker

logger = logging.getLogger("MultiAspectSampler")
logger.setLevel(os.environ.get("SIMPLETUNER_LOG_LEVEL", "WARNING"))

pil_logger = logging.getLogger("PIL.Image")
pil_logger.setLevel(logging.WARNING)
pil_logger = logging.getLogger("PIL.PngImagePlugin")
pil_logger.setLevel(logging.WARNING)
pil_logger = logging.getLogger("PIL.TiffImagePlugin")
pil_logger.setLevel(logging.WARNING)


class MultiAspectSampler(torch.utils.data.Sampler):
    def __init__(
        self,
        bucket_manager: BucketManager,
        data_backend: BaseDataBackend,
        batch_size: int,
        seen_images_path: str,
        state_path: str,
        debug_aspect_buckets: bool = False,
        delete_unwanted_images: bool = False,
        minimum_image_size: int = None,
        resolution: int = 1024,
    ):
        """
        Initializes the sampler with provided settings.
        Parameters:
        - bucket_manager: An initialised instance of BucketManager.
        - batch_size: Number of samples to draw per batch.
        - seen_images_path: Path to store the seen images.
        - state_path: Path to store the current state of the sampler.
        - debug_aspect_buckets: Flag to log state for debugging purposes.
        - delete_unwanted_images: Flag to decide whether to delete unwanted (small) images or just remove from the bucket.
        - minimum_image_size: The minimum pixel length of the smallest side of an image.
        """
        self.bucket_manager = bucket_manager
        self.data_backend = data_backend
        self.current_bucket = None
        self.batch_size = batch_size
        self.seen_images_path = seen_images_path
        self.state_path = state_path
        if debug_aspect_buckets:
            logger.setLevel(logging.DEBUG)
        self.delete_unwanted_images = delete_unwanted_images
        self.minimum_image_size = minimum_image_size
        self.resolution = resolution
        self.load_states(
            state_path=state_path,
        )
        self.change_bucket()

    def save_state(self, state_path: str = None):
        """
        This method should be called when the accelerator save hook is called,
         so that the state is correctly restored with a given checkpoint.
        """
        state = {
            "aspect_ratio_bucket_indices": self.bucket_manager.aspect_ratio_bucket_indices,
            "buckets": self.buckets,
            "exhausted_buckets": self.exhausted_buckets,
            "batch_size": self.batch_size,
            "current_bucket": self.current_bucket,
            "seen_images": self.seen_images,
            "current_epoch": self.current_epoch,
        }
        self.state_manager.save_state(state, state_path)

    def load_states(self, state_path: str):
        try:
            self.state_manager = BucketStateManager(state_path, self.seen_images_path)
            self.seen_images = self.state_manager.load_seen_images()
            self.buckets = self.load_buckets()
            previous_state = self.state_manager.load_state()
        except Exception as e:
            raise e
        self.exhausted_buckets = []
        if "exhausted_buckets" in previous_state:
            self.exhausted_buckets = previous_state["exhausted_buckets"]
        self.current_epoch = 1
        if "current_epoch" in previous_state:
            self.current_epoch = previous_state["current_epoch"]
        if "seen_images" in previous_state:
            self.seen_images = previous_state["seen_images"]
        self.log_state()

    def load_buckets(self):
        return list(
            self.bucket_manager.aspect_ratio_bucket_indices.keys()
        )  # These keys are a float value, eg. 1.78.

    def _yield_random_image(self):
        bucket = random.choice(self.buckets)
        image_path = random.choice(
            self.bucket_manager.aspect_ratio_bucket_indices[bucket]
        )
        return image_path

    def _bucket_name_to_id(self, bucket_name: str) -> int:
        """
        Return a bucket array index, by its name.

        Args:
            bucket_name (str): Bucket name, eg. "1.78"
        Returns:
            int: Bucket array index, eg. 0
        """
        return self.buckets.index(str(bucket_name))

    def _reset_buckets(self):
        if len(self.seen_images) == 0 and len(self._get_unseen_images()) == 0:
            raise Exception("No images found in the dataset.")
        logger.info(
            f"Resetting seen image list and refreshing buckets. State before reset:"
        )
        self.log_state()
        # All buckets are exhausted, so we will move onto the next epoch.
        self.current_epoch += 1
        self.exhausted_buckets = []
        self.buckets = self.load_buckets()
        self.seen_images = {}
        self.change_bucket()

    def _get_unseen_images(self, bucket=None):
        """
        Get unseen images from the specified bucket.
        If bucket is None, get unseen images from all buckets.
        """
        if bucket:
            return [
                image
                for image in self.bucket_manager.aspect_ratio_bucket_indices[bucket]
                if not self.bucket_manager.is_seen(image)
            ]
        else:
            unseen_images = []
            for b, images in self.bucket_manager.aspect_ratio_bucket_indices.items():
                unseen_images.extend(
                    [image for image in images if not self.bucket_manager.is_seen(image)]
                )
            return unseen_images

    def _yield_random_image_if_not_training(self):
        """
        If not in training mode, yield a random image and return True. Otherwise, return False.
        """
        if not StateTracker.status_training():
            return self._yield_random_image()
        return False

    def _handle_bucket_with_insufficient_images(self, bucket):
        """
        Handle buckets with insufficient images. Return True if we changed or reset the bucket.
        """
        if (
            len(self.bucket_manager.aspect_ratio_bucket_indices[bucket])
            < self.batch_size
        ):
            if bucket not in self.exhausted_buckets:
                self.move_to_exhausted()
            self.change_bucket()
            return True
        logging.debug(
            f"Bucket {bucket} has sufficient ({len(self.bucket_manager.aspect_ratio_bucket_indices[bucket])}) images."
        )
        return False

    def _reset_if_not_enough_unseen_images(self):
        """
        Reset the seen images if there aren't enough unseen images across all buckets to form a batch.
        Return True if we reset the seen images, otherwise return False.
        This is distinctly separate behaviour from change_bucket, which resets based on exhausted buckets.
        """
        total_unseen_images = sum(
            len(self._get_unseen_images(bucket)) for bucket in self.buckets
        )

        if total_unseen_images < self.batch_size:
            self._reset_buckets()
            return True
        return False

    def _get_next_bucket(self):
        """
        Get the next bucket excluding the exhausted ones.
        If all buckets are exhausted, first reset the seen images and exhausted buckets.
        """
        available_buckets = [
            bucket for bucket in self.buckets if bucket not in self.exhausted_buckets
        ]
        if not available_buckets:
            self._reset_buckets()
            available_buckets = self.buckets

        next_bucket = random.choice(available_buckets)
        return next_bucket

    def change_bucket(self):
        """
        Change the current bucket to a new one and exclude exhausted buckets from consideration.
        During _get_next_bucket(), if all buckets are exhausted, reset the exhausted list and seen images.
        """
        next_bucket = self._get_next_bucket()
        self.current_bucket = self._bucket_name_to_id(next_bucket)

    def move_to_exhausted(self):
        bucket = self.buckets[self.current_bucket]
        self.exhausted_buckets.append(bucket)
        self.buckets.remove(bucket)
        logger.debug(
            f"Bucket {bucket} is empty or doesn't have enough samples for a full batch. Moving to the next bucket."
        )
        self.log_state()

    def log_state(self):
        logger.debug(
            f'Active Buckets: {", ".join(self.convert_to_human_readable(float(b), self.bucket_manager.aspect_ratio_bucket_indices[b], self.resolution) for b in self.buckets)}'
        )
        logger.debug(
            f'Exhausted Buckets: {", ".join(self.convert_to_human_readable(float(b), self.bucket_manager.aspect_ratio_bucket_indices.get(b, "N/A"), self.resolution) for b in self.exhausted_buckets)}'
        )
        logger.info(
            "Training Statistics:\n"
            f"    -> Seen images: {len(self.seen_images)}\n"
            f"    -> Unseen images: {len(self._get_unseen_images())}\n"
            f"    -> Current Bucket: {self.current_bucket}\n"
            f"    -> Buckets: {self.buckets}\n"
            f"    -> Batch size: {self.batch_size}\n"
        )

    def _process_single_image(self, image_path, bucket):
        """
        Validate and process a single image.
        Return the image path if valid, otherwise return None.
        """
        if not self.data_backend.exists(image_path):
            logger.warning(f"Image path does not exist: {image_path}")
            self.bucket_manager.remove_image(image_path, bucket)
            return None

        try:
            logger.debug(f"AspectBucket is loading image: {image_path}")
            image_data = self.data_backend.read(image_path)
            with Image.open(BytesIO(image_data)) as image:
                if (
                    image.width < self.minimum_image_size
                    or image.height < self.minimum_image_size
                ):
                    image.close()
                    self.bucket_manager.handle_small_image(
                        image_path=image_path,
                        bucket=bucket,
                        delete_unwanted_images=self.delete_unwanted_images,
                    )
                    return None

                image = exif_transpose(image)
                aspect_ratio = round(image.width / image.height, 2)
            actual_bucket = str(aspect_ratio)
            if actual_bucket != bucket:
                self.bucket_manager.handle_incorrect_bucket(
                    image_path, bucket, actual_bucket
                )
                return None

            return image_path
        except:
            logger.warning(f"Image was bad or in-progress: {image_path}")
            return None

    def _validate_and_yield_images_from_samples(self, samples, bucket):
        """
        Validate and yield images from given samples. Return a list of valid image paths.
        """
        to_yield = []
        for image_path in samples:
            processed_image_path = self._process_single_image(image_path, bucket)
            if processed_image_path:
                to_yield.append(processed_image_path)
                if StateTracker.status_training():
                    self.seen_images[processed_image_path] = bucket
        return to_yield

    def __iter__(self):
        """
        Iterate over the sampler to yield image paths in batches.
        """
        batch_accumulator = []  # Initialize an empty list to accumulate images for a batch
        while True:
            # If not in training mode, yield a random image immediately
            early_yield = self._yield_random_image_if_not_training()
            if early_yield:
                yield early_yield
                continue

            all_buckets_exhausted = True  # Initial assumption
            for idx, bucket in enumerate(self.buckets):
                available_images = self._get_unseen_images(bucket)
                while len(available_images) >= self.batch_size:
                    all_buckets_exhausted = False  # Found a non-exhausted bucket
                    samples = random.sample(available_images, k=self.batch_size)
                    to_yield = self._validate_and_yield_images_from_samples(samples, bucket)

                    batch_accumulator.extend(to_yield)
                    # If the batch is full, yield it
                    if len(batch_accumulator) >= self.batch_size:
                        for example in batch_accumulator:
                            yield example
                        # Change bucket after a full batch is yielded
                        self.change_bucket()
                        batch_accumulator = []
                        # Break out of the while loop:
                        break

                    logger.debug(f'Updating available image list after yielding batch')
                    # Update available images after yielding
                    available_images = self._get_unseen_images(bucket)

                # Handle exhausted bucket
                if len(available_images) < self.batch_size and idx == len(self.buckets) - 1:
                    self.log_state()
                    self.move_to_exhausted()
                    self.change_bucket()

            if all_buckets_exhausted:
                self._reset_buckets()

    def __len__(self):
        return sum(
            len(indices)
            for indices in self.bucket_manager.aspect_ratio_bucket_indices.values()
        )

    @staticmethod
    def convert_to_human_readable(
        aspect_ratio_float: float, bucket: iter, resolution: int = 1024
    ):
        from math import gcd

        if aspect_ratio_float < 1:
            ratio_width = resolution
            ratio_height = int(resolution / aspect_ratio_float)
        else:
            ratio_width = int(resolution * aspect_ratio_float)
            ratio_height = resolution

        # Return the aspect ratio as a string in the format "width:height"
        return f"{aspect_ratio_float} ({len(bucket)} samples)"
        return f"{ratio_width}:{ratio_height}"
